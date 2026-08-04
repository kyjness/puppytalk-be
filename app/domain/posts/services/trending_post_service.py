import logging
from typing import Any
from uuid import UUID

from pydantic import BaseModel, TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.posts.repository import PostsRepository
from app.domain.posts.schemas import TrendingPostResponse
from app.infra.cache import get_or_compute_json

log = logging.getLogger(__name__)

# 집계 창은 서버 고정 24h(ADR 0004) — 매개변수로 남기면 캐시 키가 값별로 분화하는
# 메커니즘이 살아남아, 파라미터를 제거한 결정이 코드 어느 층에서도 강제되지 않는다.
_WINDOW_HOURS = 24
# 24h time-decay 결과가 이보다 적으면 7일·좋아요순 fallback (데이터 부족 완화).
_MIN_POSTS_FOR_TIME_DECAY = 3
_FALLBACK_WINDOW_HOURS = 24 * 7

# 차단 무관 랭킹을 캐시하고(사용자별 캐시 폭발 회피), 차단 필터는 요청별로 오버레이한다.
# 풀은 최대 limit(10)의 headroom 배수만큼 담아, 차단 저자가 상위에 있어도 limit을 채운다.
_MAX_LIMIT = 10
_POOL_SIZE = _MAX_LIMIT * 3
_CACHE_TTL_SECONDS = 300


class _TrendingCacheItem(BaseModel):
    """캐시 직렬화용 평면 스키마. author_id는 차단 오버레이 전용(응답엔 노출 안 함)."""

    id: UUID
    title: str
    category_id: int | None = None
    comment_count: int = 0
    like_count: int = 0
    view_count: int = 0
    author_id: UUID | None = None


_POOL_ADAPTER = TypeAdapter(list[_TrendingCacheItem])


class TrendingPostService:
    @classmethod
    async def get_trending_posts(
        cls,
        *,
        db: AsyncSession,
        redis_client: Any | None = None,
        limit: int = 10,
        category_id: int | None = None,
        current_user_id: UUID | None = None,
    ) -> list[TrendingPostResponse]:
        # 캐시 키는 카테고리만 — limit·유저는 키에서 제외(풀을 공유하고 사후 슬라이스/필터).
        cache_key = f"cache:trending_posts:{category_id if category_id else 'all'}"

        async def loader() -> list[_TrendingCacheItem]:
            return await cls._compute_pool(db=db, category_id=category_id)

        pool = await get_or_compute_json(
            redis=redis_client,
            key=cache_key,
            lock_key=f"{cache_key}:lock",
            ttl_seconds=_CACHE_TTL_SECONDS,
            adapter=_POOL_ADAPTER,
            loader=loader,
            cache_name="trending_posts",
        )

        # 차단 오버레이: 내가 차단한 저자의 글을 캐시된 풀에서 제거한 뒤 limit만큼 자른다.
        # 세션은 autobegin=False이므로 조회도 명시적 트랜잭션 안에서 수행한다.
        if current_user_id is not None and pool:
            async with db.begin():
                blocked = await PostsRepository.get_blocked_author_ids(current_user_id, db=db)
            if blocked:
                pool = [it for it in pool if it.author_id not in blocked]

        return [
            TrendingPostResponse(
                id=it.id,
                title=it.title,
                category_id=it.category_id,
                comment_count=it.comment_count,
                like_count=it.like_count,
                view_count=it.view_count,
            )
            for it in pool[:limit]
        ]

    @classmethod
    async def _compute_pool(
        cls, *, db: AsyncSession, category_id: int | None
    ) -> list[_TrendingCacheItem]:
        """차단 무관(current_user_id=None) 랭킹 풀을 3단 fallback으로 계산한다."""
        async with db.begin():
            posts = await PostsRepository.get_trending_posts(
                db=db,
                limit=_POOL_SIZE,
                window_hours=_WINDOW_HOURS,
                category_id=category_id,
                current_user_id=None,
                use_time_decay=True,
            )
            if len(posts) < _MIN_POSTS_FOR_TIME_DECAY:
                log.debug(
                    "trending_posts sparse in %sh (%s rows); fallback to %sh like-order",
                    _WINDOW_HOURS,
                    len(posts),
                    _FALLBACK_WINDOW_HOURS,
                )
                posts = await PostsRepository.get_trending_posts(
                    db=db,
                    limit=_POOL_SIZE,
                    window_hours=_FALLBACK_WINDOW_HOURS,
                    category_id=category_id,
                    current_user_id=None,
                    use_time_decay=False,
                )
            if len(posts) == 0:
                log.debug("trending_posts still empty; fallback to all-time like-order")
                posts = await PostsRepository.get_trending_posts(
                    db=db,
                    limit=_POOL_SIZE,
                    window_hours=None,
                    category_id=category_id,
                    current_user_id=None,
                    use_time_decay=False,
                )

        return [
            _TrendingCacheItem(
                id=p.id,
                title=p.title,
                category_id=p.category_id,
                comment_count=p.comment_count,
                like_count=p.like_count,
                view_count=p.view_count,
                author_id=p.user_id,
            )
            for p in posts
        ]
