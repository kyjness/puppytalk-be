import logging
import re
import time
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.common import split_page
from app.common.exceptions import (
    ConcurrentUpdateException,
    InvalidRequestException,
    PostNotFoundException,
)
from app.core.config import settings
from app.core.ids import new_ulid_str, parse_public_id_value
from app.core.metrics import VIEW_BUFFER_FLUSHED_VIEWS
from app.domain.comments.repository import CommentsRepository
from app.domain.likes.model import PostLikesRepository
from app.domain.media.model import MediaRepository
from app.domain.posts.schemas import PostCreateRequest, PostResponse, PostUpdateRequest
from app.infra.lock import release_lock, try_acquire_lock
from app.infra.redis import RedisLike, bulk_to_str, merge_hash_into, rename_if_exists

from ..repository import PostsRepository, validate_search_query

log = logging.getLogger(__name__)

VIEW_BUFFER_KEY = "views:{v}:buffer"
VIEW_FLUSH_LOCK_KEY = "views:{v}:flush:lock"

_HASHTAG_ALLOWED_RE = re.compile(r"[^0-9a-z가-힣_]")

# 카테고리는 시드 전용(런타임 생성 경로 없음)이라 인프로세스 TTL 캐시가 안전하다 —
# 카테고리 필터 목록·작성·수정마다 나가던 존재 검증 1쿼리를 없앤다.
_CATEGORY_CACHE_TTL_SECONDS = 300.0
_category_cache: tuple[float, frozenset[int]] | None = None


def _normalize_hashtags(raw: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in raw:
        s = (v or "").strip()
        if s.startswith("#"):
            s = s[1:].strip()
        s = "".join(s.lower().split())
        s = _HASHTAG_ALLOWED_RE.sub("", s)[:50]
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


async def _category_exists(category_id: int, db: AsyncSession) -> bool:
    global _category_cache
    now = time.monotonic()
    if _category_cache is None or now >= _category_cache[0]:
        ids = await PostsRepository.get_category_ids(db=db)
        if not ids:
            # 시드 전 빈 테이블은 캐시하지 않는다 — 마이그레이션 직후 TTL 동안 전부 400이 되는
            # 윈도우 방지. 시드가 끝나면 다음 요청부터 정상 캐시.
            return False
        _category_cache = (now + _CATEGORY_CACHE_TTL_SECONDS, ids)
    return category_id in _category_cache[1]


def _reset_category_cache() -> None:
    """테스트 격리용 — 모듈 전역 카테고리 캐시 무효화."""
    global _category_cache
    _category_cache = None


async def _validate_refs(
    category_id: int | None, image_ids: list[UUID] | None, db: AsyncSession
) -> None:
    """작성·수정 공통 참조 검증: 카테고리 존재 + 이미지 id 전량 유효."""
    if category_id is not None and not await _category_exists(category_id, db):
        raise InvalidRequestException("존재하지 않는 카테고리입니다.")
    if image_ids:
        images = await MediaRepository.get_images_by_ids(image_ids, db=db)
        if {i.id for i in images} != set(image_ids):
            raise InvalidRequestException("업로드되지 않은 이미지 ID를 참조할 수 없습니다.")


def _view_redis_key(post_id: UUID, viewer_key: str) -> str:
    return f"view:post:{post_id}:viewer:{viewer_key}"


async def _track_view(
    post_id: UUID, viewer_key: str, redis_client: RedisLike | None
) -> tuple[bool, int]:
    """조회 1건 처리 후 (writer DB 직접 증가 필요 여부, 버퍼 pending)을 돌려준다.

    dedup(SET NX)과 버퍼(HINCRBY/HGET)는 별도 명령으로 둔다 — dedup 키는 의도적으로
    해시 태그가 없어(ADR 0007) `{v}` 버퍼 키와 한 Lua에 묶으면 Cluster에서 CROSSSLOT이다.
    HINCRBY 반환값이 곧 pending이라 2왕복이면 충분하다(구 구현은 HGET 1회가 더 있었다).
    Redis 부재·실패는 fail-open — 조회는 인정하되 writer DB 직접 증가로 폴백.
    """
    if redis_client is None:
        return True, 0
    # settings를 직접 읽는다 — 모듈 상수로 스냅샷하면 설정의 진실이 두 곳이 돼
    # 테스트·런타임 재설정이 조용히 무시된다.
    ttl_seconds = settings.VIEW_CACHE_TTL_SECONDS
    field = str(post_id)
    try:
        if ttl_seconds <= 0:
            # 0 이하 = dedup 끔(같은 viewer도 매 조회 집계 — 로컬/데모 전용). 버퍼 누적은 유지.
            return False, int(await redis_client.hincrby(VIEW_BUFFER_KEY, field, 1))
        created = await redis_client.set(
            _view_redis_key(post_id, viewer_key), "1", nx=True, ex=ttl_seconds
        )
        if created:
            return False, int(await redis_client.hincrby(VIEW_BUFFER_KEY, field, 1))
        raw = await redis_client.hget(VIEW_BUFFER_KEY, field)
        return False, int(raw) if raw is not None else 0
    except Exception as e:
        log.warning("조회수 추적 Redis 오류(Fail-open, writer 직접 증가): %s", e)
        return True, 0


async def _get_buffer_pending(redis_client: RedisLike | None, post_id: UUID) -> int:
    if redis_client is None:
        return 0
    try:
        raw = await redis_client.hget(VIEW_BUFFER_KEY, str(post_id))
        return int(raw) if raw is not None else 0
    except Exception as e:
        log.warning("조회수 버퍼 HGET 실패(Fail-open 0): %s", e)
        return 0


class PostService:
    @classmethod
    async def create_post(
        cls,
        user_id: UUID,
        data: PostCreateRequest,
        db: AsyncSession,
    ) -> UUID:
        async with db.begin():
            await _validate_refs(data.category_id, data.image_ids, db)
            hashtags = _normalize_hashtags(data.hashtags) if data.hashtags is not None else None
            return await PostsRepository.create_post(
                user_id,
                data.title,
                data.content,
                data.image_ids,
                category_id=data.category_id,
                hashtag_names=hashtags,
                db=db,
            )

    @classmethod
    async def get_posts(
        cls,
        size: int,
        db: AsyncSession,
        q: str | None = None,
        category_id: int | None = None,
        current_user_id: UUID | None = None,
        cursor: UUID | None = None,
    ) -> tuple[list[PostResponse], bool]:
        search = validate_search_query(q)
        async with db.begin():
            await _validate_refs(category_id, None, db)
            fetched = await PostsRepository.get_all_posts(
                size,
                db=db,
                cursor=cursor,
                search=search,
                category_id=category_id,
                current_user_id=current_user_id,
            )
            posts, has_more = split_page(fetched, size)
            liked_ids: set[UUID] = set()
            if current_user_id is not None and posts:
                liked_ids = await PostLikesRepository.get_liked_post_ids_for_user(
                    current_user_id, [p.id for p in posts], db=db
                )
            result = [
                PostResponse.model_validate(p).model_copy(update={"is_liked": p.id in liked_ids})
                for p in posts
            ]
        return result, has_more

    @classmethod
    async def get_post_detail(
        cls,
        post_id: UUID,
        db: AsyncSession,
        current_user_id: UUID | None = None,
        *,
        viewer_key: str,
        redis_client: RedisLike | None = None,
        writer_db: AsyncSession | None = None,
    ) -> PostResponse:
        async with db.begin():
            found = await PostsRepository.get_post_detail(
                post_id, db=db, current_user_id=current_user_id
            )
            if found is None:
                raise PostNotFoundException()
            post, is_liked = found
            data = PostResponse.model_validate(post).model_copy(update={"is_liked": is_liked})

        # writer_db가 없으면 읽기 전용 — 조회를 집계하지 않고 pending만 반영한다.
        extra_db = 0
        if writer_db is not None:
            direct_db, pending = await _track_view(post_id, viewer_key, redis_client)
            if direct_db:
                async with writer_db.begin():
                    try:
                        await PostsRepository.increment_view_count(post_id, db=writer_db)
                    except StaleDataError as e:
                        raise ConcurrentUpdateException() from e
                extra_db = 1
        else:
            pending = await _get_buffer_pending(redis_client, post_id)
        return data.model_copy(update={"view_count": data.view_count + pending + extra_db})

    @classmethod
    async def flush_view_counts_to_db(cls, redis_client: RedisLike | None) -> None:
        if redis_client is None:
            return
        lock_value: str | None = None
        drain_key = f"views:{{v}}:drain:{new_ulid_str()}"
        try:
            lock_value = await try_acquire_lock(
                redis_client, VIEW_FLUSH_LOCK_KEY, settings.VIEW_FLUSH_LOCK_SECONDS
            )
            if lock_value is None:
                return
            if not await rename_if_exists(redis_client, VIEW_BUFFER_KEY, drain_key):
                return
            fields = await redis_client.hgetall(drain_key)
            if not fields:
                await redis_client.delete(drain_key)
                return
            from app.db.session import get_connection

            flushed_views = 0
            try:
                async with get_connection() as db:
                    async with db.begin():
                        for pid, cnt_raw in fields.items():
                            delta = int(cnt_raw)
                            if delta > 0:
                                pk = bulk_to_str(pid) or ""
                                await PostsRepository.increment_view_count(
                                    parse_public_id_value(pk), db=db, delta=delta
                                )
                                flushed_views += delta
            except Exception:
                # DB 트랜잭션이 롤백된 경우에만 재병합해야 이중 집계가 없다.
                await merge_hash_into(redis_client, drain_key, VIEW_BUFFER_KEY)
                raise
            # 커밋 성공분만 계측(롤백 시 위에서 raise되어 여기 안 옴).
            VIEW_BUFFER_FLUSHED_VIEWS.inc(flushed_views)
            # 커밋 성공 후에는 delta가 이미 durable하므로 drain 삭제 실패는 재병합하면 안 된다
            # (재병합 시 커밋분을 다시 더해 이중 집계). best-effort 삭제 — 실패해도 유실 없음.
            try:
                await redis_client.delete(drain_key)
            except Exception as e:
                log.warning("조회수 flush drain 삭제 실패(집계는 반영됨, stale 키만 잔존): %s", e)
        finally:
            if lock_value is not None:
                await release_lock(redis_client, VIEW_FLUSH_LOCK_KEY, lock_value)

    @classmethod
    async def update_post(
        cls,
        post_id: UUID,
        data: PostUpdateRequest,
        db: AsyncSession,
    ) -> None:
        async with db.begin():
            await _validate_refs(data.category_id, data.image_ids, db)
            hashtags = _normalize_hashtags(data.hashtags) if data.hashtags is not None else None
            try:
                found = await PostsRepository.update_post(
                    post_id,
                    title=data.title,
                    content=data.content,
                    image_ids=data.image_ids,
                    category_id=data.category_id,
                    hashtag_names=hashtags,
                    expected_version=data.version,
                    db=db,
                )
            except StaleDataError as e:
                raise ConcurrentUpdateException() from e
            if not found:
                raise PostNotFoundException()

    @classmethod
    async def delete_post(cls, post_id: UUID, db: AsyncSession) -> None:
        """게시글 삭제 + 댓글 soft-delete·좋아요 삭제 캐스케이드를 단일 트랜잭션에서 조율."""
        async with db.begin():
            if not await PostsRepository.soft_delete(post_id, db=db):
                raise PostNotFoundException()
            await CommentsRepository.soft_delete_by_post(post_id, db=db)
            await PostLikesRepository.delete_by_post_id(post_id, db=db)
