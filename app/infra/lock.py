# Redis 분산 락 공용 프리미티브: 획득(SET NX EX + 랜덤 토큰) → 해제(값 비교 CAS Lua).
# 장애 정책은 호출부 몫 — 획득의 Redis 오류는 그대로 전파한다(캐시=loader 폴백,
# 배치 잡=락 없이 진행, 조회수 flush=해당 회차 스킵처럼 호출부마다 다르다).

import hashlib
import logging
import secrets

from app.infra.redis import RedisLike, eval_script_cached

log = logging.getLogger(__name__)

# 내가 건 락일 때만 삭제 — TTL 만료 후 남의 락을 지우는 사고 방지.
RELEASE_LOCK_LUA = (
    "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) "
    "else return 0 end"
)
_RELEASE_LOCK_SHA = hashlib.sha1(RELEASE_LOCK_LUA.encode()).hexdigest()


async def try_acquire_lock(redis: RedisLike, key: str, ttl_seconds: int) -> str | None:
    """획득 성공 시 해제용 토큰, 이미 점유 중이면 None. Redis 오류는 전파."""
    token = secrets.token_urlsafe(24)
    acquired = bool(await redis.set(key, token, nx=True, ex=ttl_seconds))
    return token if acquired else None


async def release_lock(redis: RedisLike, key: str, token: str) -> None:
    """CAS 해제. 실패는 경고만 — TTL 만료가 최종 안전망이라 본편 흐름을 깨지 않는다."""
    try:
        await eval_script_cached(redis, RELEASE_LOCK_LUA, _RELEASE_LOCK_SHA, 1, key, token)
    except Exception as e:
        log.warning("lock release failed key=%s err=%s", key, e)
