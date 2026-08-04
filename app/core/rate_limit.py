# 분산 fixed-window rate limit 프리미티브 (HTTP 미들웨어·WS 수신 루프·도메인 라우터 공용).
# Redis Lua로 INCR+EXPIRE+TTL 원자 수행, 장애 시 경로 성격에 따라 통과(fail-open) 또는
# 인스턴스 로컬 메모리 윈도 폴백. HTTP 경로별 정책은 미들웨어(core.middleware.rate_limit) 소유.
import hashlib
import logging
import time
from typing import Any

from app.core.metrics import RATE_LIMIT_REJECTIONS
from app.infra.redis import RedisLike, eval_script_cached

logger = logging.getLogger(__name__)

_KEY_PREFIX = "rl"

# In-memory Fallback: 최대 10,000키, OOM 방지 eviction.
_MEMORY_MAX_KEYS = 10_000
_memory_store: dict[str, tuple[int, float]] = {}

_LUA_FIXED_WINDOW = """
local c = redis.call('INCR', KEYS[1])
if c == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
return {c, ttl}
"""
# 앱에서 가장 잦은 Redis 호출(매 요청) — EVALSHA로 스크립트 전문 전송을 피한다.
_LUA_FIXED_WINDOW_SHA = hashlib.sha1(_LUA_FIXED_WINDOW.encode()).hexdigest()


def _memory_evict_if_needed(now: float) -> None:
    """저장소가 최대 키 수 이상이면: 만료된 키 삭제 후, 여전히 초과 시 window_end_ts가 가장 작은 키 삭제."""
    if len(_memory_store) < _MEMORY_MAX_KEYS:
        return
    expired = [k for k, (_, end) in _memory_store.items() if end < now]
    for k in expired:
        del _memory_store[k]
    while len(_memory_store) >= _MEMORY_MAX_KEYS and _memory_store:
        oldest_key = min(_memory_store.keys(), key=lambda k: _memory_store[k][1])
        del _memory_store[oldest_key]


def _check_memory_fixed_window(key: str, window_sec: int, max_count: int) -> tuple[bool, int]:
    """In-memory Fixed Window. (allowed, retry_after_seconds)."""
    now = time.monotonic()
    _memory_evict_if_needed(now)
    if key not in _memory_store:
        _memory_store[key] = (1, now + window_sec)
        return True, 0
    count, window_end = _memory_store[key]
    if now >= window_end:
        _memory_store[key] = (1, now + window_sec)
        return True, 0
    count += 1
    _memory_store[key] = (count, window_end)
    if count > max_count:
        retry_after = max(0, int(window_end - now))
        return False, retry_after
    return True, 0


async def _check_redis_fixed_window(
    redis: RedisLike,
    key: str,
    window_sec: int,
    max_count: int,
) -> tuple[bool, int]:
    full_key = f"{_KEY_PREFIX}:{key}"
    result: Any = await eval_script_cached(
        redis, _LUA_FIXED_WINDOW, _LUA_FIXED_WINDOW_SHA, 1, full_key, window_sec
    )
    count, ttl = int(result[0]), int(result[1])
    retry_after = max(0, ttl) if ttl >= 0 else window_sec
    if count > max_count:
        return False, retry_after
    return True, 0


async def check_fixed_window(
    redis: RedisLike | None,
    key: str,
    *,
    window_sec: int,
    max_count: int,
    fail_open: bool = False,
) -> tuple[bool, int]:
    """fixed-window 검사 단일 진입점(미들웨어·WS 수신 루프 공용). (allowed, retry_after).

    Redis 우선(멀티 인스턴스 공유 한도). Redis 부재·장애 시: `fail_open=True`면 통과
    (글로벌 한도 — 가용성 우선), False면 인스턴스 로컬 메모리 윈도로 폴백(로그인·업로드·
    WS 같은 남용 방어 경로 — 근사 한도라도 유지).

    key는 `종류:식별자` 규약 — 첫 콜론 앞이 거부 메트릭의 limit 라벨이 되므로
    카디널리티가 유한한 접두사를 쓸 것(login·signup_upload·global·chat 등).
    """
    allowed = True
    retry_after = 0
    if redis is not None:
        try:
            allowed, retry_after = await _check_redis_fixed_window(
                redis, key, window_sec, max_count
            )
        except Exception as e:
            logger.warning(
                "Rate limit Redis 오류: %s. %s.", e, "통과" if fail_open else "메모리 폴백"
            )
            if not fail_open:
                allowed, retry_after = _check_memory_fixed_window(key, window_sec, max_count)
    elif not fail_open:
        allowed, retry_after = _check_memory_fixed_window(key, window_sec, max_count)
    if not allowed:
        count_rejection(key.split(":", 1)[0])
    return allowed, retry_after


def count_rejection(limit: str) -> None:
    """거부 계측 단일 창구. check_fixed_window를 거치지 않는 거부(WS 억제 창의 로컬
    즉시 거부 등)도 반드시 이 함수로 센다 — 아니면 스팸 급증 구간에서 메트릭이
    거부의 대부분을 놓쳐 대시보드가 '한도가 거의 안 걸린다'고 오판하게 된다."""
    RATE_LIMIT_REJECTIONS.labels(limit=limit).inc()


class LocalRejectionGate:
    """연결/세션 단위 로컬 거부 게이트 — check_fixed_window 앞단의 억제 창.

    거부되면 retry_after 동안 Redis 왕복 없이 로컬에서 즉시 거부해(스팸의 공유 Redis 부하
    증폭 방지), 억제 창을 피해서 페이싱하든 말든 **연속 거부 누계**가 임계에 닿으면
    should_close=True로 연결 종료를 지시한다. 계측 라벨은 check_fixed_window와 동일하게
    key의 첫 콜론 앞 접두사에서 파생 — 한 거부 메트릭에 라벨 정의처가 둘이 되지 않게 한다.
    """

    def __init__(self, key: str, *, close_threshold: int) -> None:
        self._key = key
        self._limit_label = key.split(":", 1)[0]
        self._close_threshold = close_threshold
        self._blocked_until = 0.0
        self._consecutive_rejections = 0

    async def check(
        self,
        redis: RedisLike | None,
        *,
        window_sec: int,
        max_count: int,
    ) -> tuple[bool, int, bool]:
        """(allowed, retry_after, should_close). 억제 창 안이면 Redis 왕복 없이 거부한다."""
        now = time.monotonic()
        if now < self._blocked_until:
            count_rejection(self._limit_label)
            self._consecutive_rejections += 1
            retry_after = int(self._blocked_until - now) + 1
            return False, retry_after, self._consecutive_rejections >= self._close_threshold
        allowed, retry_after = await check_fixed_window(
            redis, self._key, window_sec=window_sec, max_count=max_count
        )
        if allowed:
            self._consecutive_rejections = 0
            return True, 0, False
        self._blocked_until = max(self._blocked_until, now + max(retry_after, 1))
        self._consecutive_rejections += 1
        return False, retry_after, self._consecutive_rejections >= self._close_threshold
