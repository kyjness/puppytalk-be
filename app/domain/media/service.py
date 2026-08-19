# 미디어 비즈니스 로직. 순수 데이터 반환·커스텀 예외. HTTP·ApiResponse 없음. Full-Async.


import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
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
    build_pending_file_key,
    build_permanent_file_key,
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
    PRESIGNED_MAX_BYTES,
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


class _ReservedUpload:
    """승격까지 끝났지만 **아직 확정되지 않은** 업로드. `_reserved_upload` 블록이 끝나면
    `image`가 확정된 행을 준다 — 블록 안에서 읽으면 아직 없다고 터진다."""

    def __init__(self, image_id: UUID) -> None:
        self.image_id = image_id
        self._image: Image | None = None

    @property
    def image(self) -> Image:
        if self._image is None:
            raise RuntimeError("upload is not confirmed yet — read .image after the block")
        return self._image


async def _keyset_cleanup(
    db: AsyncSession,
    *,
    fetch: Callable[[UUID | None, int], Awaitable[list[Image]]],
    on_delete_failed: Callable[[Image, Exception], None],
) -> int:
    """이미지 정리 공통 루프. **배치당 트랜잭션 하나** — keyset(id > last_id)으로 행을 잠근 채
    조회(`FOR UPDATE SKIP LOCKED`) → 스토리지 삭제 → 성공분 행 삭제 → 커밋. 반환 = 실제 삭제 수.

    스토리지 삭제를 트랜잭션 **안**에서 하는 이유: 잠금을 놓고 지우면 그 사이 확정 요청이
    예약 행을 확정하거나 첨부 검증이 이미지를 물 수 있고, 그 뒤의 행 삭제가 그걸 무너뜨린다.
    잠근 채 지우면 두 배우가 겹칠 수 없다 — 그들은 이 행을 기다렸다가 "없음"을 보거나
    (확정 → 500, 객체는 만들어지지 않았다), 우리가 잠긴 행을 건너뛴다. 배치 하나(≤200건)의
    S3 삭제 동안 그 행들만 잠기고, 어차피 지워질 행이라 대기자는 저 둘뿐이다.

    스토리지 삭제 실패분도 커서를 넘겨 이번 실행에선 건너뛰고 다음 실행에서 재시도한다(실패
    이미지가 id 앞머리에 쌓여 뒤쪽 정상 행을 굶기는 것을 방지). 잠겨서 건너뛴 행은 페이지에
    안 나오므로 커서가 그 위를 지나가도 다음 실행이 다시 본다.
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
    @asynccontextmanager
    async def _reserved_upload(
        cls,
        file_key: str,
        *,
        purpose: str,
        expected_size: int | None,
        uploader_id: UUID | None,
        db: AsyncSession,
    ) -> AsyncIterator[_ReservedUpload]:
        """예약 행 → (잠근 채) S3 승격 → **블록 본문** → 확정. 블록이 끝나면 `.image`가 채워진다.

        **행이 먼저다.** 목적지 키를 미리 정해 `deleted_at`을 찍은 예약 행으로 커밋한 뒤
        승격한다. 승격이든 그 뒤든 실패하면 예약 상태 그대로 두고 예외를 올린다 — 스위퍼가
        S3 객체와 행을 함께 회수하므로 **행 없는 객체가 생길 수 없다**. 보상 삭제
        (`storage_delete`)를 쓰지 않는 이유가 이것이다. 보상은 그 자체로 실패할 수 있고,
        실패하면 행 기준 스위퍼가 원리적으로 못 보는 객체가 영구히 남는다(ADR 0019).

        **승격부터 확정까지 예약 행을 `FOR UPDATE`로 잡고 있는다.** 유예를 넘긴 예약 행을
        스위퍼가 집는 순간 확정이 성공하면, 순서에 따라 200 받은 이미지의 행이 지워지거나
        객체만 지워지거나 행 없는 객체가 남는다. 잠금이 그 셋을 전부 막는다 — 스위퍼는
        `SKIP LOCKED`로 잠긴 행을 건너뛰고, 스위퍼가 먼저 잡았으면 우리는 기다렸다가 행이
        없어진 걸 보고 **승격 전에** 실패한다(객체는 만들어지지 않는다). 대가는 이 트랜잭션이
        S3 copy 동안 열려 있다는 것.

        확정이 블록 **뒤**인 이유: 확정 뒤에 할 일이 남은 호출부(가입은 토큰 발급이 남는다)가
        있으면 그게 실패했을 때 되돌리는 쓰기가 필요해진다 — 방금 없앤 보상 쓰기가 S3 대신
        DB로 되살아난다. 블록 본문이 실패하면 예약 상태로 롤백되고 스위퍼가 회수한다.
        """
        key = file_key.strip().lstrip("/")
        if not is_valid_pending_file_key(key):
            raise InvalidRequestException(message="Invalid or expired pending file_key.")
        # 승격 전 검증 — 거부는 pending/에서 끝나야 한다(그쪽은 lifecycle이 회수한다).
        try:
            meta = await head_pending_object(key)
        except ValueError as e:
            raise InvalidImageFileException(message="Uploaded object is missing or invalid.") from e
        size = int(meta.get("ContentLength") or 0)
        if size < 1 or size > PRESIGNED_MAX_BYTES:
            raise InvalidImageFileException(message="Uploaded object is missing or invalid.")
        if expected_size is not None and expected_size != size:
            raise InvalidImageFileException(message="Reported size does not match stored object.")
        content_type = validate_image_content_type(str(meta.get("ContentType") or ""))
        etag = str(meta.get("ETag") or "")
        if not etag:
            raise InvalidImageFileException(message="Uploaded object is missing or invalid.")

        dest_key = build_permanent_file_key(purpose, content_type)
        async with db.begin():
            image = await MediaRepository.create_reserved_image(
                file_key=dest_key,
                file_url=build_url(dest_key),
                content_type=content_type,
                size=size,
                uploader_id=uploader_id,
                db=db,
            )
            image_id = image.id

        async with db.begin():
            if await MediaRepository.lock_reserved_image(image_id, db=db) is None:
                # 잠그기 전에 스위퍼가 회수해 갔다(유예를 넘길 만큼 지연된 경우). 되살리지 않는다.
                raise InternalServerErrorException("Upload was reclaimed before confirmation.")
            # presign URL이 살아 있는 동안 head~copy 사이 재업로드로 위 검증을 우회할 수 있다.
            # copy에 첫 HEAD의 ETag를 조건으로 걸어 그 경우 copy 자체가 실패하게 한다 —
            # 승격 후 재검증이 필요 없고, 검증한 그 객체만 승격된다.
            try:
                await promote_pending_object(key, dest_key, etag=etag)
            except ValueError as e:
                raise InvalidImageFileException(
                    message="Uploaded object is missing or invalid."
                ) from e

            upload = _ReservedUpload(image_id)
            yield upload
            confirmed = await MediaRepository.confirm_reserved_image(
                image_id, size=size, content_type=content_type, db=db
            )
            if confirmed is None:  # 잠근 채라 일어날 수 없지만, 조용히 넘기지 않는다
                raise InternalServerErrorException("Upload was reclaimed before confirmation.")
            upload._image = confirmed

    @classmethod
    async def confirm_presigned_upload(
        cls,
        body: ConfirmUploadRequest,
        user_id: UUID,
        db: AsyncSession,
    ) -> ImageUploadResponse:
        # purpose 값 검증은 스키마의 Literal["profile", "post"]가 담당한다.
        async with cls._reserved_upload(
            body.file_key,
            purpose=body.purpose,
            expected_size=body.size,
            uploader_id=user_id,
            db=db,
        ) as upload:
            pass  # 확정 전에 할 일이 없다
        return ImageUploadResponse.model_validate(upload.image)

    @classmethod
    async def confirm_presigned_signup_upload(
        cls,
        body: ConfirmSignupUploadRequest,
        db: AsyncSession,
        redis: RedisLike | None,
    ) -> SignupImageUploadData:
        # uploader_id는 아직 없다(가입 전) — 가입이 토큰으로 귀속시킨다.
        async with cls._reserved_upload(
            body.file_key,
            purpose="signup",
            expected_size=body.size,
            uploader_id=None,
            db=db,
        ) as upload:
            # 토큰이 먼저다(블록 안 = 확정 전). 토큰 없이 확정된 이미지는 영영 귀속될 수 없는데,
            # 확정을 먼저 하면 그 상태를 **되돌리는 쓰기**가 필요해진다. 여기서 실패하면 예약
            # 상태로 롤백되고 스위퍼가 회수한다 — 되돌릴 것이 없다.
            signup_token = await cls.issue_upload_token(upload.image_id, redis=redis)
        return SignupImageUploadData(
            id=upload.image.id,
            file_url=upload.image.file_url,
            signup_token=signup_token,
        )

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
        """요청 안에서는 **DB만** 건드린다 — 소프트 삭제 + 참조 해제, 트랜잭션 하나.

        스토리지 삭제는 주기 스위퍼가 뒤에서 한다. S3와 DB는 한 트랜잭션으로 묶을 수 없어
        요청 안에서 둘 다 건드리면 "앞은 됐는데 뒤가 실패"가 반드시 존재하고, 그 실패가
        사용자에게 재시도 요구로 나갔다. 이제 실패는 재시도가 공짜인 백그라운드에서만 난다
        (backlog #44, ADR 0019).

        수거해야 한다는 사실이 `deleted_at`으로 DB에 남으므로 스위퍼가 스스로 찾아낸다 —
        따로 알릴 대상이 없어 알림이 유실될 표면 자체가 없다(ADR 0018의 등급 판단).
        """
        async with db.begin():
            if not await MediaRepository.soft_delete_image_if_owned(image_id, user_id, db=db):
                # 없거나·남의 것이거나·이미 지워졌다. 셋을 구분해 알리지 않는다(존재 노출).
                raise ImageNotFoundException()

    @classmethod
    async def sweep_unused_images(cls, db: AsyncSession) -> int:
        """어디에도 연결되지 않은 이미지 정리 — 버려진 업로드(24시간+)와 소프트 삭제분.

        `deleted_at`이 찍힌 행은 24시간을 기다리지 않는다 — 지운 이미지가 하루 동안 S3에 남아
        있으면 안 되고, 참조는 삭제 시점에 이미 끊겨 있다. 다만 확정 중인 예약 행을 지키려
        짧은 유예를 둔다.

        **두 종류를 따로 훑는다.** 조건을 OR로 합치면 `created_at`에 인덱스가 없어 전체 스캔이
        되고, 소프트 삭제분용 부분 인덱스가 무용지물이 된다. 나눠 두면 그쪽은 인덱스를 탄다.

        **락은 잡지 않는다** — 배타 실행은 호출부가 JOB_LOCK_SWEEP_UNUSED로 보장한다.
        여기서 또 잡으면 러너가 이미 그 키를 쥔 상태라 SET NX가 항상 실패해(재진입 불가)
        정리가 영구 no-op이 된다.
        """

        async def _fetch_abandoned(after_id: UUID | None, limit: int) -> list[Image]:
            return await MediaRepository.get_abandoned_images(
                older_than_hours=24, db=db, limit=limit, after_id=after_id
            )

        async def _fetch_reclaimable(after_id: UUID | None, limit: int) -> list[Image]:
            return await MediaRepository.get_reclaimable_images(
                grace_seconds=settings.RESERVED_IMAGE_GRACE_SECONDS,
                db=db,
                limit=limit,
                after_id=after_id,
            )

        def _on_fail(img: Image, e: Exception) -> None:
            logger.warning(
                "Sweep storage delete failed image_id=%s file_key=%s: %s",
                img.id,
                img.file_key,
                e,
            )

        return await _keyset_cleanup(
            db, fetch=_fetch_abandoned, on_delete_failed=_on_fail
        ) + await _keyset_cleanup(db, fetch=_fetch_reclaimable, on_delete_failed=_on_fail)

    @classmethod
    async def sweep_unused_images_detached(cls, redis: RedisLike | None) -> None:
        """BackgroundTasks용 sweep — 응답 후 실행되므로 요청 스코프 세션 대신 자체 세션을 연다.

        주기 잡과 **같은 키**로 배타 실행한다 — 주기 잡 쪽은 러너(PeriodicJob.lock_key)가,
        이 요청 유발 경로는 여기서 직접 건다. 실패는 클라이언트에 재전달하지 않는다
        (202는 '시작'만 보장) — 로그만 남긴다.
        """
        acquired, lock_value = await try_acquire_job_lock(
            redis, key=JOB_LOCK_SWEEP_UNUSED, ttl_seconds=_JOB_LOCK_TTL_SECONDS
        )
        if not acquired:
            logger.info("skip media sweep: 다른 경로가 실행 중")
            return
        try:
            async with get_connection() as db:
                deleted_count = await cls.sweep_unused_images(db=db)
            logger.info("media_sweep_done deleted_count=%s", deleted_count)
        except Exception:
            logger.warning("media_sweep_failed", exc_info=True)
        finally:
            if lock_value and redis is not None:
                await release_lock(redis, JOB_LOCK_SWEEP_UNUSED, lock_value)

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
