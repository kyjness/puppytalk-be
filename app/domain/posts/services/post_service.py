import logging
import re
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.common.exceptions import (
    ConcurrentUpdateException,
    InvalidImageException,
    InvalidRequestException,
    PostNotFoundException,
)
from app.core.config import settings
from app.core.ids import new_ulid_str, parse_public_id_value
from app.core.metrics import VIEW_BUFFER_FLUSHED_VIEWS
from app.domain.likes.model import PostLikesModel
from app.domain.media.model import MediaModel
from app.domain.posts.schemas import PostCreateRequest, PostResponse, PostUpdateRequest
from app.infra.lock import release_lock, try_acquire_lock
from app.infra.redis import RedisLike, bulk_to_str

from ..repository import PostsModel, validate_search_query

log = logging.getLogger(__name__)

VIEW_BUFFER_KEY = "views:{v}:buffer"
VIEW_FLUSH_LOCK_KEY = "views:{v}:flush:lock"
_RENAME_BUFFER_TO_DRAIN_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return 0
end
redis.call('RENAME', KEYS[1], KEYS[2])
return 1
"""

_HASHTAG_ALLOWED_RE = re.compile(r"[^0-9a-z가-힣_]")


def _normalize_hashtags(raw: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in raw:
        s = (v or "").strip()
        if not s:
            continue
        if s.startswith("#"):
            s = s[1:].strip()
        s = s.lower()
        s = "".join(s.split())
        s = _HASHTAG_ALLOWED_RE.sub("", s)
        if not s:
            continue
        if len(s) > 50:
            s = s[:50]
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _view_redis_key(post_id: UUID, viewer_key: str) -> str:
    return f"view:post:{post_id}:viewer:{viewer_key}"


async def _consume_view_if_new_redis(
    post_id: UUID, viewer_key: str, redis_client: RedisLike | None
) -> bool:
    # settings를 직접 읽는다 — 모듈 상수로 스냅샷하면 설정의 진실이 두 곳이 돼
    # 테스트·런타임 재설정이 조용히 무시된다.
    ttl_seconds = settings.VIEW_CACHE_TTL_SECONDS
    if ttl_seconds <= 0:
        return True  # 0 이하 = dedup 끔(같은 viewer도 매 조회 집계 — 로컬/데모 전용)
    if redis_client is None:
        return True
    key = _view_redis_key(post_id, viewer_key)
    try:
        created = await redis_client.set(key, "1", nx=True, ex=ttl_seconds)
        return bool(created)
    except Exception as e:
        log.warning("조회수 dedup Redis 오류(Fail-open, 증가 허용): %s", e)
        return True


async def _get_buffer_pending(redis_client: RedisLike | None, post_id: UUID) -> int:
    if redis_client is None:
        return 0
    try:
        raw = await redis_client.hget(VIEW_BUFFER_KEY, str(post_id))
        return int(raw) if raw is not None else 0
    except Exception as e:
        log.warning("조회수 버퍼 HGET 실패(Fail-open 0): %s", e)
        return 0


async def _try_view_increment_in_buffer(post_id: UUID, redis_client: RedisLike | None) -> bool:
    try:
        if redis_client is None:
            raise ConnectionError("redis unavailable")
        await redis_client.hincrby(VIEW_BUFFER_KEY, str(post_id), 1)
        return True
    except Exception as e:
        log.warning("조회수 버퍼 HINCRBY 실패 Fail-open DB: %s", e)
        return False


async def _apply_view_increment(
    post_id: UUID,
    viewer_key: str,
    redis_client: RedisLike | None,
    writer_db: AsyncSession,
) -> bool:
    """조회수 증가 안무: dedup → 버퍼 누적 → (버퍼 불가 시) writer 직접 증가 폴백.

    반환 = writer DB에 직접 +1했는가. 버퍼에 흡수된 증가분은 flush 전까지 DB에 없으므로,
    호출자가 응답 view_count를 보정할 때 pending(버퍼 잔량)과 이 반환값을 함께 쓴다."""
    if not await _consume_view_if_new_redis(post_id, viewer_key, redis_client):
        return False
    if await _try_view_increment_in_buffer(post_id, redis_client):
        return False
    async with writer_db.begin():
        try:
            await PostsModel.increment_view_count(post_id, db=writer_db)
        except StaleDataError as e:
            raise ConcurrentUpdateException() from e
    return True


class PostService:
    @classmethod
    async def create_post(
        cls,
        user_id: UUID,
        data: PostCreateRequest,
        db: AsyncSession,
    ) -> UUID:
        async with db.begin():
            if data.category_id is not None:
                ok = await PostsModel.category_exists(data.category_id, db=db)
                if not ok:
                    raise InvalidRequestException("존재하지 않는 카테고리입니다.")
            if data.image_ids:
                images = await MediaModel.get_images_by_ids(data.image_ids, db=db)
                if set(i.id for i in images) != set(data.image_ids):
                    raise InvalidImageException()
            hashtags = _normalize_hashtags(data.hashtags) if data.hashtags is not None else None
            post_id = await PostsModel.create_post(
                user_id,
                data.title,
                data.content,
                data.image_ids,
                category_id=data.category_id,
                hashtag_names=hashtags,
                db=db,
            )
            return post_id

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
        search_q = validate_search_query(q)
        async with db.begin():
            if category_id is not None:
                ok = await PostsModel.category_exists(category_id, db=db)
                if not ok:
                    raise InvalidRequestException("존재하지 않는 카테고리입니다.")
            fetched = await PostsModel.get_all_posts(
                size,
                db=db,
                cursor=cursor,
                search_q=search_q,
                category_id=category_id,
                current_user_id=current_user_id,
            )
            has_more = len(fetched) > size
            posts = fetched[:size]
            liked_ids: set[UUID] = set()
            if current_user_id is not None and posts:
                liked_ids = await PostLikesModel.get_liked_post_ids_for_user(
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
            if current_user_id is not None:
                post_with_like = await PostsModel.get_post_by_id_with_like_flag(
                    post_id, current_user_id, db=db
                )
                if not post_with_like:
                    raise PostNotFoundException()
                post, is_liked = post_with_like
                data = PostResponse.model_validate(post).model_copy(update={"is_liked": is_liked})
            else:
                post = await PostsModel.get_post_by_id(post_id, db=db, current_user_id=None)
                if not post:
                    raise PostNotFoundException()
                data = PostResponse.model_validate(post)

        extra_db = 0
        if writer_db is not None and await _apply_view_increment(
            post_id, viewer_key, redis_client, writer_db
        ):
            extra_db = 1

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
            renamed = await redis_client.eval(
                _RENAME_BUFFER_TO_DRAIN_LUA, 2, VIEW_BUFFER_KEY, drain_key
            )
            if not int(renamed):
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
                                await PostsModel.increment_view_count_delta(
                                    parse_public_id_value(pk), delta, db=db
                                )
                                flushed_views += delta
            except Exception:
                # DB 트랜잭션이 롤백된 경우에만 재병합해야 이중 집계가 없다.
                await cls._merge_drain_into_buffer(redis_client, drain_key, VIEW_BUFFER_KEY)
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

    @staticmethod
    async def _merge_drain_into_buffer(
        redis_client: RedisLike, drain_key: str, buffer_key: str
    ) -> None:
        fields = await redis_client.hgetall(drain_key)
        if not fields:
            await redis_client.delete(drain_key)
            return
        for pid, cnt_raw in fields.items():
            await redis_client.hincrby(buffer_key, pid, int(cnt_raw))
        await redis_client.delete(drain_key)

    @classmethod
    async def update_post(
        cls,
        post_id: UUID,
        data: PostUpdateRequest,
        db: AsyncSession,
    ) -> None:
        fs = data.model_fields_set
        async with db.begin():
            post = await PostsModel.get_post_by_id(post_id, db=db)
            if not post:
                raise PostNotFoundException()
            if "version" in fs and data.version is not None:
                if data.version != post.version:
                    raise ConcurrentUpdateException(
                        "다른 사용자가 이미 글을 수정했습니다. 최신 데이터를 확인해주세요."
                    )
            title = data.title if "title" in fs else None
            content = data.content if "content" in fs else None
            image_ids = data.image_ids if "image_ids" in fs else None
            category_id = data.category_id if "category_id" in fs else None
            hashtags_raw = data.hashtags if "hashtags" in fs else None
            if category_id is not None:
                ok = await PostsModel.category_exists(category_id, db=db)
                if not ok:
                    raise InvalidRequestException("존재하지 않는 카테고리입니다.")
            if image_ids is not None:
                images = await MediaModel.get_images_by_ids(image_ids, db=db)
                if set(i.id for i in images) != set(image_ids):
                    raise InvalidImageException()
            hashtags = _normalize_hashtags(hashtags_raw) if hashtags_raw is not None else None
            try:
                delta = await PostsModel.update_post(
                    post_id,
                    title=title,
                    content=content,
                    image_ids=image_ids,
                    category_id=category_id,
                    hashtag_names=hashtags,
                    db=db,
                )
            except StaleDataError as e:
                raise ConcurrentUpdateException() from e

            if delta is None:
                raise PostNotFoundException()

    @classmethod
    async def delete_post(cls, post_id: UUID, db: AsyncSession) -> None:
        async with db.begin():
            success, _image_ids = await PostsModel.delete_post(post_id, db=db)
            if not success:
                raise PostNotFoundException()
