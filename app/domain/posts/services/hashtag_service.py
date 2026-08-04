import logging
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.posts.schemas import TrendingHashtagResponse
from app.infra.cache import get_or_compute_json

from ..repository import PostsRepository

log = logging.getLogger(__name__)

_TRENDING_LIST_ADAPTER = TypeAdapter(list[TrendingHashtagResponse])


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
                rows = await PostsRepository.get_trending_hashtags(db=db, limit=limit)
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
