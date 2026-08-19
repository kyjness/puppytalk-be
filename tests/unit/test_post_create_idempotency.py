"""POST /posts 멱등성 훅 단위 테스트 (ADR 0008).

핵심 불변식: **in-flight 락은 내가 잡은 것만 해제한다.** 직접 `SET NX` 후 `DEL`로 풀면
락 TTL이 만료된 뒤 뒤늦게 끝난 요청이 *다음* 요청의 락을 지워, 같은 멱등성 키로 두 건이
동시에 생성될 수 있다. 그래서 공용 프리미티브(`app/infra/lock.py`)의 랜덤 토큰 + CAS 해제를
쓴다 — ADR 0007이 조회수 flush에서 같은 이유로 단순 delete를 기각한 것과 같은 판단이다.

fail-open 구간(Redis 부재·오류)에서는 락을 잡은 적이 없으므로 해제도 시도하지 않는다.
"""

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from app.common.exceptions import ConcurrentUpdateException
from app.domain.posts.idempotency import (
    post_create_idempotency_after_failure,
    post_create_idempotency_after_success,
    post_create_idempotency_before,
)

from tests.unit.fakes import FakeRedis

pytestmark = pytest.mark.asyncio

_KEY = "idem-key-0001"


def _req(redis: Any) -> Any:
    """`get_app_redis(request.app)`가 보는 최소 표면만 갖춘 가짜 요청."""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=redis)))


def _lock_key(fp: str) -> str:
    return f"idemp:post:create:lock:{fp}"


class _Resp:
    """`_store_success_result`가 부르는 model_dump만 갖춘 가짜 응답."""

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return {"code": "OK", "data": {"id": "abc"}, "requestId": "r1"}


async def test_late_finisher_does_not_release_another_requests_lock():
    """락 TTL이 만료된 뒤 뒤늦게 끝난 요청이 **다음 요청의 락**을 지우면 안 된다.

    지우면 같은 멱등성 키로 세 번째 요청이 락을 잡아, 진행 중인 두 번째와 동시에
    게시글을 만든다 — 멱등성이 막으려던 바로 그 중복이다.
    """
    redis = FakeRedis()
    req = _req(redis)
    uid = uuid.uuid4()

    _, a = await post_create_idempotency_before(req, uid, _KEY)
    assert a is not None and a.lock_token is not None

    # TTL 만료를 흉내낸다 — 키가 사라지고 다음 요청이 새 토큰으로 잡는다.
    redis.kv.pop(_lock_key(a.fingerprint))
    _, b = await post_create_idempotency_before(req, uid, _KEY)
    assert b is not None and b.lock_token is not None
    assert b.lock_token != a.lock_token

    # 뒤늦게 끝난 A가 해제를 시도한다.
    await post_create_idempotency_after_failure(req, a)

    assert redis.kv.get(_lock_key(a.fingerprint)) == b.lock_token, (
        "A가 B의 락을 지웠다 — 같은 키로 동시 생성이 가능해진다"
    )


async def test_same_key_in_flight_raises_conflict():
    """처리 중인 같은 키는 409로 돌려보낸다(ADR 0008 결정 4)."""
    redis = FakeRedis()
    req = _req(redis)
    uid = uuid.uuid4()

    _, first = await post_create_idempotency_before(req, uid, _KEY)
    assert first is not None and first.lock_token is not None

    with pytest.raises(ConcurrentUpdateException):
        await post_create_idempotency_before(req, uid, _KEY)


async def test_after_success_stores_result_and_releases_own_lock():
    redis = FakeRedis()
    req = _req(redis)
    uid = uuid.uuid4()

    _, h = await post_create_idempotency_before(req, uid, _KEY)
    assert h is not None
    await post_create_idempotency_after_success(req, h, cast(Any, _Resp()))

    assert _lock_key(h.fingerprint) not in redis.kv, "성공 후 자기 락은 풀어야 한다"
    assert f"idemp:post:create:res:{h.fingerprint}" in redis.kv


async def test_fail_open_without_redis_never_touches_lock():
    """Redis 부재 구간에선 락을 잡은 적이 없으므로 해제도 시도하지 않는다."""
    req = _req(None)
    uid = uuid.uuid4()

    _, h = await post_create_idempotency_before(req, uid, _KEY)
    assert h is not None
    assert h.lock_token is None, "잡지 않은 락의 토큰이 있으면 남의 락을 지우게 된다"

    # 해제 경로가 예외 없이 no-op이어야 한다.
    await post_create_idempotency_after_failure(req, h)


async def test_no_header_skips_idempotency_entirely():
    """헤더가 없으면 opt-in이 아니므로 핸들 자체가 없다(ADR 0008 결정 1)."""
    redis = FakeRedis()
    cached, h = await post_create_idempotency_before(_req(redis), uuid.uuid4(), None)
    assert cached is None and h is None
    assert redis.kv == {}


class _SetThenTimeoutRedis(FakeRedis):
    """SET NX가 서버에는 적용됐는데 응답만 늦어 클라이언트가 타임아웃을 받는 상황."""

    async def set(self, key, val, nx=False, ex=None):
        await super().set(key, val, nx=nx, ex=ex)
        raise TimeoutError("reply exceeded socket_timeout")


async def test_lock_set_but_reply_timed_out_is_still_released():
    """락은 서버에 잡혔는데 응답만 늦어 예외가 난 경우에도 **끝나면 풀려야 한다.**

    소켓 타임아웃 도입으로 새로 열린 경로다. 예외를 fail-open으로 처리하면서 토큰을 버리면
    아무도 그 락을 못 풀어 TTL(120s) 동안 같은 키의 재시도가 전부 409가 된다 — 옛
    무조건 DEL은 이 경우를 자가치유했다. 토큰을 쥐고 있으면 CAS 해제는 안전하다(내 토큰일
    때만 지운다).
    """
    redis = _SetThenTimeoutRedis()
    req = _req(redis)
    uid = uuid.uuid4()

    cached, h = await post_create_idempotency_before(req, uid, _KEY)
    assert cached is None and h is not None
    assert _lock_key(h.fingerprint) in redis.kv, "전제: 서버에는 락이 잡혀 있다"

    await post_create_idempotency_after_failure(req, h)

    assert _lock_key(h.fingerprint) not in redis.kv, (
        "응답만 늦은 락을 안 풀면 TTL 동안 같은 키가 전부 409다"
    )
