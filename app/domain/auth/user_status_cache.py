# users.status의 Redis Cache-Aside 조각(refresh·access 검증 공용).
# auth 서비스와 인증 의존성(api.dependencies.auth)이 함께 쓰는 계약이라 공개 모듈로 둔다.

import logging
from uuid import UUID

from app.infra.redis import RedisLike

logger = logging.getLogger(__name__)

_CACHE_PREFIX = "user:status:"
# 짧은 TTL: 정지/탈퇴 반영 지연과 스테일 허용 폭의 트레이드오프(분산 무효화와 함께 사용).
USER_STATUS_CACHE_TTL_SECONDS = 240


def user_status_cache_key(user_id: UUID) -> str:
    return f"{_CACHE_PREFIX}{user_id}"


async def set_user_status_cache_best_effort(
    redis_client: RedisLike,
    user_id: UUID,
    status_value: str,
) -> None:
    """로그인 등 확실히 ACTIVE인 시점에 캐시를 채워 첫 refresh DB 조회를 줄인다."""
    try:
        await redis_client.set(
            user_status_cache_key(user_id),
            status_value,
            ex=USER_STATUS_CACHE_TTL_SECONDS,
        )
    except Exception as e:
        logger.warning("user status cache SET failed user_id=%s err=%s", user_id, e)


async def invalidate_user_status_cache(redis_client: RedisLike | None, user_id: UUID) -> None:
    """``users.status`` 변경(정지·해제·탈퇴 등) 후 캐시를 제거한다.

    Redis 장애 시 로그만 남기고 무시해 본편 트랜잭션을 막지 않는다.
    """
    if redis_client is None:
        return
    try:
        await redis_client.delete(user_status_cache_key(user_id))
    except Exception as e:
        logger.warning("user status cache DEL failed user_id=%s err=%s", user_id, e)
