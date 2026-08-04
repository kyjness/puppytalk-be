# users.status의 Redis Cache-Aside 조각(refresh·access 검증 공용).
# auth 서비스와 인증 의존성(api.dependencies.auth)이 함께 쓰는 계약이라 공개 모듈로 둔다.

import json
import logging
from typing import Any
from uuid import UUID

from app.infra.redis import RedisLike, bulk_to_str

logger = logging.getLogger(__name__)

_CACHE_PREFIX = "user:status:"
_AUTH_CACHE_PREFIX = "user:auth:"
# 짧은 TTL: 정지/탈퇴 반영 지연과 스테일 허용 폭의 트레이드오프(분산 무효화와 함께 사용).
USER_STATUS_CACHE_TTL_SECONDS = 240


def user_status_cache_key(user_id: UUID) -> str:
    return f"{_CACHE_PREFIX}{user_id}"


def user_auth_cache_key(user_id: UUID) -> str:
    return f"{_AUTH_CACHE_PREFIX}{user_id}"


async def get_auth_cache(redis_client: RedisLike, user_id: UUID) -> dict[str, Any] | None:
    """인증 스냅샷({"status", "user"}) 조회. 장애·파싱 실패는 미스 취급(fail-open)."""
    try:
        raw = bulk_to_str(await redis_client.get(user_auth_cache_key(user_id)))
        if raw is None:
            return None
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception as e:
        logger.warning("user auth cache GET fail-open user_id=%s err=%s", user_id, e)
        return None


async def set_auth_cache_best_effort(
    redis_client: RedisLike,
    user_id: UUID,
    *,
    status_value: str,
    user_payload: dict[str, Any],
) -> None:
    """인증 스냅샷 저장 — 히트 시 상태 판정·CurrentUser 조립까지 DB 없이 끝나게 한다."""
    try:
        await redis_client.set(
            user_auth_cache_key(user_id),
            json.dumps({"status": status_value, "user": user_payload}, ensure_ascii=False),
            ex=USER_STATUS_CACHE_TTL_SECONDS,
        )
    except Exception as e:
        logger.warning("user auth cache SET failed user_id=%s err=%s", user_id, e)


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
    """``users.status`` 변경(정지·해제·탈퇴 등)이나 인증 스냅샷에 실리는 프로필
    (닉네임·프로필 이미지) 변경 후 status·auth 캐시를 함께 제거한다.

    Redis 장애 시 로그만 남기고 무시해 본편 트랜잭션을 막지 않는다.
    """
    if redis_client is None:
        return
    try:
        await redis_client.delete(user_status_cache_key(user_id), user_auth_cache_key(user_id))
    except Exception as e:
        logger.warning("user status cache DEL failed user_id=%s err=%s", user_id, e)
