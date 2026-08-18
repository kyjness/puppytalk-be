"""`/readyz`가 쓰는 DB 프로브가 유한 시간 안에 판정하는지 (ADR 0006).

엔진의 `connect_timeout`은 **연결 수립**만 덮는다. 풀에 이미 있는 커넥션을 재사용하면
적용되지 않으므로, DB가 접속은 되는데 응답만 멎으면 `SELECT 1`이 무한 대기한다. 그러면
readiness 프로브가 503 대신 매달려 원인이 프로브 타임아웃 뒤로 숨고, 호출마다 writer·reader
커넥션을 붙잡아 풀을 잠식한다.

여기의 가짜는 **psycopg만큼 고약해야 한다.** psycopg 3.x `AsyncConnection.wait`는 첫
`CancelledError`를 잡아 서버 취소를 시도한 뒤 **같은 쿼리를 다시 기다린다**(두 번째 취소만
통과시킨다). 취소를 순순히 받는 가짜로는 `asyncio.wait_for` 한 번이 통과해 버려, 실제
드라이버에서 매달리는 결함을 못 잡는다. 또 `pool_pre_ping`의 `SELECT 1`은 `engine.connect()`
**안**에서 돌므로 `connect()`도 매달릴 수 있어야 한다.

단언은 "몇 초 걸리는가"가 아니라 **"유한 시간 안에 not ready로 판정하는가"** 에 둔다.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from app.core.config import settings
from app.db import connection as conn_mod

pytestmark = pytest.mark.asyncio

# 무한이 아님을 증명하는 것이 목적이라 넉넉히 잡는다. 수정 전 코드는 영원히 매달린다.
_BUDGET_SEC = 5.0
# 실제로 기다리는 시간 — 프로덕션 기본값(1s)을 그대로 쓸 이유가 없다. 단언은 동일하다.
_TEST_PING_TIMEOUT = 0.1


async def _hang_like_psycopg() -> None:
    """응답 없는 DB — 첫 취소는 삼키고(서버 취소 시도 흉내) 다시 기다린다."""
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        await asyncio.Event().wait()  # 두 번째 취소만 여기서 올라간다


class _HangingConn:
    async def execute(self, _stmt: Any) -> Any:
        await _hang_like_psycopg()


class _HangingEngine:
    """`async with engine.connect()`만 흉내내는 가짜 엔진. 쿼리 단계에서 매달린다."""

    @contextlib.asynccontextmanager
    async def connect(self) -> AsyncIterator[_HangingConn]:
        yield _HangingConn()


class _HangingOnConnectEngine(_HangingEngine):
    """`pool_pre_ping`처럼 **checkout 단계**에서 매달리는 가짜 엔진."""

    @contextlib.asynccontextmanager
    async def connect(self) -> AsyncIterator[_HangingConn]:
        await _hang_like_psycopg()
        yield _HangingConn()  # pragma: no cover — 도달하지 않는다


@pytest.mark.parametrize(
    "engine_cls",
    [_HangingEngine, _HangingOnConnectEngine],
    ids=["query_hangs", "checkout_hangs"],
)
async def test_check_database_reports_not_ready_in_bounded_time(monkeypatch, engine_cls):
    monkeypatch.setattr(settings, "DB_PING_TIMEOUT", _TEST_PING_TIMEOUT)
    monkeypatch.setattr(conn_mod, "writer_engine", engine_cls())
    monkeypatch.setattr(conn_mod, "reader_engine", engine_cls())

    # wait_for를 쓰면 이 테스트 자체가 가짜의 "첫 취소 삼킴"에 갇힌다 — 판정은 wait로,
    # 정리는 이중 취소로 한다(결함이 남은 코드에서 테스트가 행이 아니라 실패로 끝나게).
    task = asyncio.ensure_future(conn_mod.check_database())
    done, _ = await asyncio.wait({task}, timeout=_BUDGET_SEC)
    if task not in done:
        for _ in range(2):
            task.cancel()
            await asyncio.wait({task}, timeout=1.0)
        pytest.fail(
            f"{_BUDGET_SEC}s 안에 판정하지 못했다 — 프로브 상한이 실제 드라이버 동작을 "
            "뚫지 못한다. /readyz가 503 대신 매달리고 커넥션을 붙잡는다."
        )
    assert task.result() is False, "응답 없는 DB는 not ready 로 판정해야 한다"
