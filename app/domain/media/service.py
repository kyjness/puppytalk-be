# 미디어 비즈니스 로직. 순수 데이터 반환·커스텀 예외. HTTP·ApiResponse 없음. Full-Async.


import logging
import secrets
from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.common.exceptions import (
    ImageNotFoundException,
    InternalServerErrorException,
    InvalidImageFileException,
    InvalidRequestException,
)
from app.core.config import settings
from app.core.ids import new_uuid7
from app.db import get_connection
from app.domain.media.image_policy import (
    CONTENT_TYPE_EXT,
    build_pending_file_key,
    sanitize_presign_filename,
    validate_image_content_type,
)
from app.domain.media.model import Image, MediaRepository
from app.domain.media.schema import (
    ConfirmSignupUploadRequest,
    ConfirmUploadRequest,
    ImageUploadResponse,
    PresignUploadRequest,
    PresignUploadResponse,
    SignupImageUploadData,
)
from app.infra.lock import release_lock, try_acquire_job_lock
from app.infra.redis import RedisLike, bulk_to_str
from app.infra.storage import (
    build_url,
    head_pending_object,
    is_valid_pending_file_key,
    issue_presigned_post,
    promote_pending_object,
    storage_delete,
)

logger = logging.getLogger(__name__)

_UPLOAD_TOKEN_KEY_PREFIX = "upload_token:"
# sweep은 주기 잡 말고 요청 유발 경로(sweep_unused_images_detached)에서도 돈다. 주기 잡이
# **같은 키**를 선언하도록 main에서 PeriodicJob.lock_key로 넘겨(키가 갈리면 두 경로가 서로를
# 배제하지 못한다), 락 획득은 여기 한 곳에서만 한다.
# signup 정리는 호출자가 주기 잡뿐이라 러너 락이 전부 덮는다 — 사설 락을 두지 않는다.
JOB_LOCK_SWEEP_UNUSED = "lock:media-sweep"
_JOB_LOCK_TTL_SECONDS = 600


async def _keyset_cleanup(
    db: AsyncSession,
    *,
    fetch: Callable[[UUID | None, int], Awaitable[list[Image]]],
    on_delete_failed: Callable[[Image, Exception], None],
) -> int:
    """이미지 정리 공통 루프. keyset(id > last_id) 배치로 조회 → 트랜잭션 밖에서 스토리지 삭제 →
    성공분만 짧은 트랜잭션으로 DB 제거. 반환 = 실제 삭제 수.

    스토리지 삭제 실패분도 커서를 넘겨 이번 실행에선 건너뛰고 다음 실행에서 재시도한다(실패
    이미지가 id 앞머리에 쌓여 뒤쪽 정상 행을 굶기는 것을 방지).
    """
    batch_size = settings.MEDIA_CLEANUP_BATCH_SIZE
    total_deleted = 0
    last_id: UUID | None = None
    while True:
        async with db.begin():
            rows = await fetch(last_id, batch_size)
        if not rows:
            break
        last_id = rows[-1].id

        deletable_ids: list[UUID] = []
        for img in rows:
            try:
                await run_in_threadpool(storage_delete, img.file_key)
                deletable_ids.append(img.id)
            except Exception as e:
                on_delete_failed(img, e)

        if deletable_ids:
            async with db.begin():
                total_deleted += await MediaRepository.delete_images_by_ids(deletable_ids, db=db)

        if len(rows) < batch_size:
            break
    return total_deleted


class MediaService:
    @classmethod
    async def issue_presigned_upload(cls, body: PresignUploadRequest) -> PresignUploadResponse:
        content_type = validate_image_content_type(body.content_type)
        safe_name = sanitize_presign_filename(body.filename, content_type)
        upload_id = new_uuid7()
        file_key = build_pending_file_key(upload_id, safe_name)
        url, fields = await issue_presigned_post(file_key, content_type)
        return PresignUploadResponse(url=url, fields=fields, file_key=file_key)

    @classmethod
    async def _confirm_pending_key(
        cls,
        file_key: str,
        *,
        purpose: str,
        expected_size: int | None,
    ) -> tuple[str, str, str, int]:
        key = file_key.strip().lstrip("/")
        if not is_valid_pending_file_key(key):
            raise InvalidRequestException(message="Invalid or expired pending file_key.")
        # 검증은 promote(영구 경로 copy + pending 삭제) 앞에서 — 승격 후 거부는 DB 행 없는
        # 영구 객체를 남겨, DB 행 기준 sweeper도 pending/ 전용 lifecycle도 지우지 못한다.
        try:
            meta = await head_pending_object(key)
        except ValueError as e:
            raise InvalidImageFileException(message="Uploaded object is missing or invalid.") from e
        size = int(meta.get("ContentLength") or 0)
        if expected_size is not None and expected_size != size:
            raise InvalidImageFileException(message="Reported size does not match stored object.")
        validate_image_content_type(str(meta.get("ContentType") or ""))
        try:
            dest_key, size, content_type = await promote_pending_object(
                key, purpose, ext_by_content_type=CONTENT_TYPE_EXT
            )
        except ValueError as e:
            raise InvalidImageFileException(message="Uploaded object is missing or invalid.") from e
        # presign URL(15분)이 살아 있는 동안 head~promote 사이 재업로드로 위 검증을 우회할 수
        # 있어 승격 결과를 재확인한다 — 실패 시 승격본을 지워 누수 없이 거부.
        try:
            if expected_size is not None and expected_size != size:
                raise InvalidImageFileException(
                    message="Reported size does not match stored object."
                )
            validate_image_content_type(content_type)
        except Exception:
            try:
                await run_in_threadpool(storage_delete, dest_key)
            except Exception:
                logger.warning("승격 후 검증 실패분 삭제 실패 dest_key=%s", dest_key)
            raise
        return dest_key, build_url(dest_key), content_type, size

    @classmethod
    async def confirm_presigned_upload(
        cls,
        body: ConfirmUploadRequest,
        user_id: UUID,
        db: AsyncSession,
    ) -> ImageUploadResponse:
        # purpose 값 검증은 스키마의 Literal["profile", "post"]가 담당한다.
        dest_key, file_url, content_type, size = await cls._confirm_pending_key(
            body.file_key,
            purpose=body.purpose,
            expected_size=body.size,
        )
        try:
            async with db.begin():
                image = await MediaRepository.create_image(
                    file_key=dest_key,
                    file_url=file_url,
                    content_type=content_type,
                    size=size,
                    uploader_id=user_id,
                    db=db,
                )
                return ImageUploadResponse.model_validate(image)
        except Exception:
            try:
                await run_in_threadpool(storage_delete, dest_key)
            except Exception as rollback_e:
                logger.warning(
                    "Rollback storage delete failed after confirm_presigned_upload DB error "
                    "file_key=%s: %s",
                    dest_key,
                    rollback_e,
                )
            raise

    @classmethod
    async def confirm_presigned_signup_upload(
        cls,
        body: ConfirmSignupUploadRequest,
        db: AsyncSession,
        redis: RedisLike | None,
    ) -> SignupImageUploadData:
        dest_key, file_url, content_type, size = await cls._confirm_pending_key(
            body.file_key,
            purpose="signup",
            expected_size=body.size,
        )
        image = None
        try:
            async with db.begin():
                image = await MediaRepository.create_temp_image(
                    file_key=dest_key,
                    file_url=file_url,
                    content_type=content_type,
                    size=size,
                    db=db,
                )
            signup_token = await cls.issue_upload_token(image.id, redis=redis)
            return SignupImageUploadData(
                id=image.id,
                file_url=image.file_url,
                signup_token=signup_token,
            )
        except Exception:
            try:
                if image is not None:
                    async with db.begin():
                        await MediaRepository.delete_images_by_ids([image.id], db=db)
                await run_in_threadpool(storage_delete, dest_key)
            except Exception as rollback_e:
                logger.warning(
                    "Rollback storage delete failed after confirm_presigned_signup_upload "
                    "file_key=%s: %s",
                    dest_key,
                    rollback_e,
                )
            raise

    @classmethod
    async def issue_upload_token(cls, image_id: UUID, redis: RedisLike | None) -> str:
        if redis is None:
            raise InternalServerErrorException("Redis unavailable for upload token issuance.")
        token = secrets.token_urlsafe(32)
        key = f"{_UPLOAD_TOKEN_KEY_PREFIX}{token}"
        # Token이 소유권 검증/첨부 단회성임을 보장하기 위해 TTL로 제한.
        await redis.set(key, str(image_id), ex=settings.SIGNUP_IMAGE_TOKEN_TTL_SECONDS)
        return token

    @classmethod
    async def verify_upload_token(cls, token: str, redis: RedisLike | None) -> UUID | None:
        if not token or redis is None:
            return None
        key = f"{_UPLOAD_TOKEN_KEY_PREFIX}{token}"
        try:
            image_id = bulk_to_str(await redis.get(key))
            if image_id is None:
                return None
            # 사용 즉시 토큰 폐기(단일 사용). 경쟁 상황은 DB 첨부 조건(uploader_id is None)로 안전하게 처리.
            await redis.delete(key)
            if not image_id:
                return None
            from app.core.ids import parse_public_id_value

            try:
                return parse_public_id_value(image_id)
            except ValueError:
                return None
        except Exception as e:
            logger.warning("verify_upload_token redis error: %s", e)
            return None

    @classmethod
    async def delete_image(cls, image_id: UUID, user_id: UUID, db: AsyncSession) -> None:
        file_key = None
        async with db.begin():
            image = await MediaRepository.get_image_by_id(image_id, db=db)
            if not image or image.uploader_id != user_id:
                raise ImageNotFoundException()
            file_key = image.file_key
            await MediaRepository.delete_image_record(image, db=db)
        if file_key:
            await run_in_threadpool(storage_delete, file_key)

    @classmethod
    async def sweep_unused_images(cls, db: AsyncSession, redis: RedisLike | None = None) -> int:
        """24시간 이상 경과 + users/dog_profiles/post_images 어디에도 연결되지 않은 이미지 정리."""
        acquired, lock_value = await try_acquire_job_lock(
            redis, key=JOB_LOCK_SWEEP_UNUSED, ttl_seconds=_JOB_LOCK_TTL_SECONDS
        )
        if not acquired:
            logger.info("skip sweep_unused_images: lock already held")
            return 0
        try:

            async def _fetch(after_id: UUID | None, limit: int) -> list[Image]:
                return await MediaRepository.get_orphan_images_older_than(
                    older_than_hours=24, db=db, limit=limit, after_id=after_id
                )

            def _on_fail(img: Image, e: Exception) -> None:
                logger.warning(
                    "Sweep storage delete failed image_id=%s file_key=%s: %s",
                    img.id,
                    img.file_key,
                    e,
                )

            return await _keyset_cleanup(db, fetch=_fetch, on_delete_failed=_on_fail)
        finally:
            if lock_value and redis is not None:
                await release_lock(redis, JOB_LOCK_SWEEP_UNUSED, lock_value)

    @classmethod
    async def sweep_unused_images_detached(cls, redis: RedisLike | None) -> None:
        """BackgroundTasks용 sweep — 응답 후 실행되므로 요청 스코프 세션 대신 자체 세션을 연다.

        redis를 받아 스케줄 잡과 같은 락(JOB_LOCK_SWEEP_UNUSED)을 공유하고, 실패는
        클라이언트에 재전달하지 않는다(202는 '시작'만 보장) — 로그만 남긴다.
        """
        try:
            async with get_connection() as db:
                deleted_count = await cls.sweep_unused_images(db=db, redis=redis)
            logger.info("media_sweep_done deleted_count=%s", deleted_count)
        except Exception:
            logger.warning("media_sweep_failed", exc_info=True)

    @classmethod
    async def cleanup_expired_signup_images(
        cls, db: AsyncSession, *, task_id: str
    ) -> tuple[int, list[str]]:
        """만료된 가입 임시 이미지 정리. 배타 실행은 주기 잡 러너의 락이 보장한다."""
        failed_file_keys: list[str] = []

        async def _fetch(after_id: UUID | None, limit: int) -> list[Image]:
            return await MediaRepository.get_expired_signup_images(
                db=db, limit=limit, after_id=after_id
            )

        def _on_fail(img: Image, e: Exception) -> None:
            logger.warning(
                "Signup image storage delete failed task_id=%s image_id=%s file_key=%s: %s",
                task_id,
                img.id,
                img.file_key,
                e,
                exc_info=True,
            )
            failed_file_keys.append(img.file_key)

        total_deleted = await _keyset_cleanup(db, fetch=_fetch, on_delete_failed=_on_fail)
        return total_deleted, failed_file_keys
