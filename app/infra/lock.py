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


async def try_acquire_job_lock(
    redis: RedisLike | None, *, key: str, ttl_seconds: int
) -> tuple[bool, str | None]:
    """배치 잡용 락 획득 — 반환은 (진행해도 되는가, 해제 토큰).

    이 모듈 상단의 장애 정책 중 "배치 잡 = 락 없이 진행"의 구현이다. Redis 부재(단일 노드
    개발)·장애는 진행으로 판정한다 — 정리 잡은 멈추는 것보다 중복 실행이 낫다.
    이미 다른 인스턴스가 점유 중이면 (False, None)으로 조용히 skip하게 한다.
    """
    if redis is None:
        return True, None
    try:
        token = await try_acquire_lock(redis, key, ttl_seconds)
        return (token is not None), token
    except Exception as e:
        log.warning("job lock unavailable key=%s fallback_without_lock err=%s", key, e)
        return True, None
