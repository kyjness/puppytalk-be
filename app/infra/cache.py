# 읽기 폭주 경로용 캐시 헬퍼(ADR 0004). get→miss 시 분산 락으로 단일 워커만 재계산(thundering
# herd 방지), 나머지는 짧게 대기 후 채워진 값을 읽는다. 락 해제는 값 비교 CAS(남의 락 미삭제).
# Redis 부재·오류는 전부 fail-open으로 loader(DB) 직조회. TypeAdapter로 직렬화 계약을 고정한다.
import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from pydantic import TypeAdapter, ValidationError

from app.core.metrics import CACHE_EVENTS
from app.infra.lock import release_lock, try_acquire_lock
from app.infra.redis import RedisLike, bulk_to_str

log = logging.getLogger(__name__)

T = TypeVar("T")

# 락 보유 워커가 재계산하는 동안 대기 워커의 폴링 파라미터(해시태그 기존 값과 동일).
_LOCK_TTL_SECONDS = 5
_WAIT_MAX_SECONDS = 2.0
_WAIT_INTERVAL_SECONDS = 0.1


def _decode(raw: Any, adapter: TypeAdapter[T], cache_name: str) -> T | None:
    text = bulk_to_str(raw)
    if not text:
        return None
    try:
        return adapter.validate_json(text)
    except ValidationError as e:
        # 스키마 불일치·손상은 캐시 미스로 처리(정상 플로우로 폴백).
        log.warning("%s cache schema mismatch (ignore): %s", cache_name, e)
        return None


async def get_or_compute_json(
    *,
    redis: RedisLike | None,
    key: str,
    lock_key: str,
    ttl_seconds: int,
    adapter: TypeAdapter[T],
    loader: Callable[[], Awaitable[T]],
    cache_name: str,
) -> T:
    """캐시 히트면 즉시 반환, 미스면 분산 락 아래 loader로 재계산·기록. Redis 부재/오류는 loader 폴백.

    - ``adapter``: 캐시 값의 직렬화 계약(TypeAdapter).
    - ``loader``: 캐시 미스 시 실제 계산(대개 DB 조회) 코루틴.
    - ``cache_name``: `cache_events_total{cache}` 라벨(hit/miss 계측).

    락 대기 타임아웃도 loader 폴백이다 — 빈 값 반환은 "틀린 데이터"라 대기자 수만큼의
    DB 쿼리(운영 봉투 내)를 감내하는 쪽을 택한다(ADR 0004).
    """
    if redis is None:
        return await loader()

    try:
        decoded = _decode(await redis.get(key), adapter, cache_name)
        if decoded is not None:
            CACHE_EVENTS.labels(cache=cache_name, result="hit").inc()
            return decoded
        CACHE_EVENTS.labels(cache=cache_name, result="miss").inc()
    except Exception as e:
        log.warning("%s cache read failed (fallback to loader): %s", cache_name, e)
        return await loader()

    try:
        lock_value = await try_acquire_lock(redis, lock_key, _LOCK_TTL_SECONDS)
    except Exception as e:
        log.warning("%s cache lock failed (fallback to loader): %s", cache_name, e)
        return await loader()

    if lock_value is None:
        waited = 0.0
        while waited < _WAIT_MAX_SECONDS:
            await asyncio.sleep(_WAIT_INTERVAL_SECONDS)
            waited += _WAIT_INTERVAL_SECONDS
            try:
                decoded2 = _decode(await redis.get(key), adapter, cache_name)
                if decoded2 is not None:
                    return decoded2
            except Exception:
                break
        return await loader()

    try:
        result = await loader()
        try:
            await redis.setex(key, ttl_seconds, adapter.dump_json(result).decode("utf-8"))
        except Exception as e:
            log.warning("%s cache write failed (ignore): %s", cache_name, e)
        return result
    finally:
        await release_lock(redis, lock_key, lock_value)
