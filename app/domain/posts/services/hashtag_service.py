import logging
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.posts.schemas import TrendingHashtagResponse
from app.infra.cache import get_or_compute_json

from ..repository import PostsRepository

log = logging.getLogger(__name__)

_TRENDING_LIST_ADAPTER = TypeAdapter(list[TrendingHashtagResponse])

# 집계 창은 트렌딩 게시글과 동일한 서버 고정 24h(ADR 0004) — 창이 서버 고정이라
# 캐시 키가 값별로 분화하지 않는다.
_WINDOW_HOURS = 24
# 24h 결과가 이보다 적으면 전체 기간으로 fallback (초기·로컬 데이터에서 빈 위젯 방지).
_MIN_HASHTAGS_FOR_WINDOW = 3


class HashtagService:
    CACHE_TRENDING_HASHTAGS_KEY = "cache:trending_hashtags"
    _TRENDING_HASHTAGS_TTL_SECONDS = 600
    _TRENDING_HASHTAGS_LOCK_KEY = "cache:trending_hashtags:lock"

    @classmethod
    async def get_trending_hashtags(
        cls,
        *,
        db: AsyncSession,
        redis_client: Any | None = None,
        limit: int = 10,
    ) -> list[TrendingHashtagResponse]:
        async def loader() -> list[TrendingHashtagResponse]:
            async with db.begin():
                rows = await PostsRepository.get_trending_hashtags(
                    db=db, window_hours=_WINDOW_HOURS, limit=limit
                )
                if len(rows) < _MIN_HASHTAGS_FOR_WINDOW:
                    log.debug(
                        "trending_hashtags sparse in %sh (%s rows); fallback to all-time",
                        _WINDOW_HOURS,
                        len(rows),
                    )
                    rows = await PostsRepository.get_trending_hashtags(
                        db=db, window_hours=None, limit=limit
                    )
            return [TrendingHashtagResponse(name=name, count=count) for name, count in rows]

        return await get_or_compute_json(
            redis=redis_client,
            key=cls.CACHE_TRENDING_HASHTAGS_KEY,
            lock_key=cls._TRENDING_HASHTAGS_LOCK_KEY,
            ttl_seconds=cls._TRENDING_HASHTAGS_TTL_SECONDS,
            adapter=_TRENDING_LIST_ADAPTER,
            loader=loader,
            cache_name="trending_hashtags",
        )
