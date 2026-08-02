"""WS DM 남용 방어 단위 테스트.

핵심 불변식: WS는 HTTP rate limit 미들웨어를 타지 않으므로 수신 루프에서 유저 단위
fixed-window(Redis 우선, 장애 시 인스턴스 로컬 폴백)로 막고, 차단 관계(방향 무관)면
방 생성·저장 전에 거부한다 — 차단 방향은 응답 문구로 노출하지 않는다.
"""

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from app.common.exceptions import ForbiddenException
from app.core.rate_limit import check_fixed_window
from app.domain.chat.schema import ChatMessageSend
from app.domain.chat.service import ChatService
from app.domain.users.model import UsersModel
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.fakes import FakeRedis as SharedFakeRedis

pytestmark = pytest.mark.asyncio


# --- check_fixed_window (공개 헬퍼) ---


class _FakeRedis(SharedFakeRedis):
    """fixed-window eval이 고정 [count, ttl]을 반환하는(또는 실패하는) 가짜."""

    def __init__(self, count: int, ttl: int = 30, fail: bool = False) -> None:
        super().__init__()
        self._count = count
        self._ttl = ttl
        self._fail = fail
        self.calls: list[tuple] = []

    async def eval(self, script: str, numkeys: int, key: str, window: int):  # pyright: ignore[reportIncompatibleMethodOverride]
        if self._fail:
            raise ConnectionError("redis down")
        self.calls.append((key, window))
        return [self._count, self._ttl]


async def test_check_fixed_window_allows_under_limit():
    redis = _FakeRedis(count=3)
    allowed, retry_after = await check_fixed_window(
        cast(Any, redis), f"t:{uuid.uuid4()}", window_sec=60, max_count=5
    )
    assert allowed and retry_after == 0


async def test_check_fixed_window_blocks_over_limit_with_retry_after():
    redis = _FakeRedis(count=6, ttl=42)
    allowed, retry_after = await check_fixed_window(
        cast(Any, redis), f"t:{uuid.uuid4()}", window_sec=60, max_count=5
    )
    assert not allowed
    assert retry_after == 42


async def test_check_fixed_window_falls_back_to_memory_on_redis_failure():
    key = f"t:{uuid.uuid4()}"
    redis = _FakeRedis(count=1, fail=True)
    for _ in range(2):
        allowed, _ = await check_fixed_window(cast(Any, redis), key, window_sec=60, max_count=2)
        assert allowed
    allowed, retry_after = await check_fixed_window(
        cast(Any, redis), key, window_sec=60, max_count=2
    )
    assert not allowed  # 메모리 폴백이 3번째 요청을 차단
    assert retry_after > 0


async def test_check_fixed_window_uses_memory_without_redis():
    key = f"t:{uuid.uuid4()}"
    allowed, _ = await check_fixed_window(None, key, window_sec=60, max_count=1)
    assert allowed
    allowed, _ = await check_fixed_window(None, key, window_sec=60, max_count=1)
    assert not allowed


async def test_check_fixed_window_fail_open_passes_on_redis_absence_and_failure():
    """글로벌 한도 경로(fail_open=True)는 Redis 부재·장애 시 검사 없이 통과."""
    key = f"t:{uuid.uuid4()}"
    for _ in range(3):
        allowed, _ = await check_fixed_window(None, key, window_sec=60, max_count=1, fail_open=True)
        assert allowed
    redis = _FakeRedis(count=1, fail=True)
    for _ in range(3):
        allowed, _ = await check_fixed_window(
            cast(Any, redis), key, window_sec=60, max_count=1, fail_open=True
        )
        assert allowed


# --- send_dm_from_ws 차단 검사 ---


class _NoopTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeDb:
    """차단 거부는 방 upsert 전이어야 하므로, DB 쿼리가 실행되면 즉시 실패한다."""

    def begin(self) -> _NoopTx:
        return _NoopTx()

    async def execute(self, *args, **kwargs):
        raise AssertionError("차단 관계에서는 방 upsert 쿼리가 실행되면 안 된다")


def _sess(db: _FakeDb) -> AsyncSession:
    return cast(AsyncSession, db)


def _patch_blocked_pair(monkeypatch, a_id, b_id):
    async def fake_get_user(user_id, *, db):
        return SimpleNamespace(status="ACTIVE")

    async def fake_block_exists(a, b, *, db):
        assert {a, b} == {a_id, b_id}
        return True

    monkeypatch.setattr(UsersModel, "get_user_by_id", fake_get_user)
    monkeypatch.setattr(UsersModel, "block_exists_between", fake_block_exists)


async def test_send_dm_rejects_blocked_relation_before_room_creation(monkeypatch):
    sender, peer = uuid.uuid4(), uuid.uuid4()
    _patch_blocked_pair(monkeypatch, sender, peer)

    with pytest.raises(ForbiddenException) as exc:
        await ChatService.send_dm_from_ws(
            _sess(_FakeDb()),
            sender_id=sender,
            payload=ChatMessageSend(peer_user_id=peer, content="hi"),
            redis=None,
        )
    # 누가 차단했는지 방향을 노출하지 않는 중립 문구
    assert "차단" not in (exc.value.message or "")


async def test_rest_room_open_rejects_blocked_relation(monkeypatch):
    """가드는 get_or_create_room 깊이에 있다 — REST 방 열기 경로도 같은 지점에서 거부."""
    me, peer = uuid.uuid4(), uuid.uuid4()
    _patch_blocked_pair(monkeypatch, me, peer)

    with pytest.raises(ForbiddenException):
        await ChatService.resolve_direct_room(_sess(_FakeDb()), user_id=me, peer_id=peer)


async def test_block_exists_between_checks_both_directions():
    """방향 무관 술어인지 쿼리 구조로 고정(blocker/blocked 양방향 OR)."""
    import inspect

    src = inspect.getsource(UsersModel.block_exists_between.__func__)
    assert src.count("or_") >= 1
    assert src.count("blocker_id == user_a") == 1
    assert src.count("blocker_id == user_b") == 1


# --- 매니저 send 타임아웃 (공용 리스너 head-of-line 차단 상한) ---


async def test_send_personal_message_disconnects_stalled_socket(monkeypatch):
    import asyncio

    from app.domain.chat import manager as manager_mod

    monkeypatch.setattr(manager_mod, "_SEND_TIMEOUT_SEC", 0.01)
    manager = manager_mod.ConnectionManager()
    uid = uuid.uuid4()

    class _StalledWs:
        def __init__(self) -> None:
            self.closed_with: int | None = None

        async def send_text(self, message: str) -> None:
            await asyncio.sleep(1)

        async def send_json(self, message) -> None:
            await asyncio.sleep(1)

        async def close(self, code: int = 1000) -> None:
            self.closed_with = code

    stalled = _StalledWs()
    ws = cast(Any, stalled)
    await manager.connect(uid, ws)
    await manager.send_personal_message(uid, "x")  # 예외 없이 타임아웃 → 등록 해제 + 종료
    assert manager._by_user == {}
    # 등록만 지우면 클라이언트가 수신만 조용히 잃는다 — 실제로 닫혀야 재연결이 뜬다
    assert stalled.closed_with == 1011


# --- WS 거부 게이트 (억제 창 + 연속 거부 종료) ---


def test_rejection_gate_suppresses_and_escalates():
    from app.api.v1.chat import ws as ws_mod

    gate = ws_mod._RejectionGate()
    assert not gate.suppressed(now=100.0)

    # 거부 → 억제 창 형성
    assert gate.register_rejection(now=100.0, retry_after=10) is False
    assert gate.suppressed(now=105.0)
    assert not gate.suppressed(now=111.0)

    # 억제 창을 피해 페이싱해도 연속 거부 누계가 임계에 닿으면 종료 지시
    for _ in range(ws_mod._REJECT_CLOSE_THRESHOLD - 2):
        assert gate.register_rejection(now=200.0, retry_after=1) is False
    assert gate.register_rejection(now=300.0, retry_after=1) is True

    # 허용 프레임이 나오면 누계 리셋
    gate.register_allowed()
    assert gate.register_rejection(now=400.0, retry_after=1) is False
