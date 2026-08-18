"""`/readyz`가 쓰는 DB 프로브가 유한 시간 안에 판정하는지 (ADR 0006).

엔진의 `connect_timeout`은 **연결 수립**만 덮는다. 풀에 이미 있는 커넥션을 재사용하면
적용되지 않으므로, DB가 접속은 되는데 응답만 멎으면 `SELECT 1`이 무한 대기한다. 그러면
readiness 프로브가 503 대신 매달려 원인이 프로브 타임아웃 뒤로 숨고, 호출마다 writer·reader
커넥션을 붙잡아 풀을 잠식한다.

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


class _HangingConn:
    async def execute(self, _stmt: Any) -> Any:
        await asyncio.Event().wait()  # 응답이 오지 않는 DB


class _HangingEngine:
    """`async with engine.connect()`만 흉내내는 가짜 엔진."""

    @contextlib.asynccontextmanager
    async def connect(self) -> AsyncIterator[_HangingConn]:
        yield _HangingConn()


async def test_check_database_reports_not_ready_in_bounded_time(monkeypatch):
    monkeypatch.setattr(settings, "DB_PING_TIMEOUT", _TEST_PING_TIMEOUT)
    monkeypatch.setattr(conn_mod, "writer_engine", _HangingEngine())
    monkeypatch.setattr(conn_mod, "reader_engine", _HangingEngine())

    try:
        ok = await asyncio.wait_for(conn_mod.check_database(), timeout=_BUDGET_SEC)
    except TimeoutError:
        pytest.fail(
            f"{_BUDGET_SEC}s 안에 판정하지 못했다 — 프로브 쿼리에 상한이 없다. "
            "/readyz가 503 대신 매달리고 커넥션을 붙잡는다."
        )
    assert ok is False, "응답 없는 DB는 not ready 로 판정해야 한다"
