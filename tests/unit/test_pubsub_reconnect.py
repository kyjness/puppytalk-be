"""공용 pubsub 리스너 재연결 단위 테스트.

핵심 불변식: 접속 실패·수신 계층 예외 1회로 리스너가 죽지 않는다 — 백오프 후 새 연결로
재구독한다(멀티 인스턴스에서 크로스 인스턴스 실시간 전달이 프로세스 재시작까지 전멸하는 것 방지).
stop_event는 백오프 대기 중에도 즉시 종료시킨다.
"""

import asyncio
import json
from typing import Any
from uuid import uuid4

import pytest
from app.infra import pubsub as pubsub_mod
from app.infra.pubsub import run_user_fanout_listener

pytestmark = pytest.mark.asyncio


class _Script:
    """연결 시도(from_url 호출)마다 소비되는 동작 시나리오 1개."""

    def __init__(
        self,
        *,
        ping_fail: bool = False,
        get_message_error: bool = False,
        messages: list[dict[str, Any]] | None = None,
        stop_after: bool = False,
    ) -> None:
        self.ping_fail = ping_fail
        self.get_message_error = get_message_error
        self.messages = list(messages or [])
        self.stop_after = stop_after


class _FakePubSub:
    def __init__(self, script: _Script, stop_event: asyncio.Event) -> None:
        self._script = script
        self._stop_event = stop_event
        self.closed = False

    async def subscribe(self, *channels: str) -> None:
        pass

    async def unsubscribe(self, *channels: str) -> None:
        pass

    async def ping(self) -> None:
        # 워치독이 유휴 시 부른다 — 이 가짜는 응답(pong)을 돌려주지 않는다.
        pass

    async def get_message(self, *, ignore_subscribe_messages: bool, timeout: float):
        if self._script.messages:
            return self._script.messages.pop(0)
        if self._script.get_message_error:
            self._script.get_message_error = False
            raise ConnectionError("connection lost")
        if self._script.stop_after:
            self._stop_event.set()
        await asyncio.sleep(
            0
        )  # 실제 폴처럼 이벤트 루프에 양보 — 안 하면 리스너 루프가 루프를 독점한다
        return None

    async def aclose(self) -> None:
        self.closed = True


class _FakeRedis:
    scripts: list[_Script] = []
    stop_event: asyncio.Event
    connect_count = 0

    def __init__(self, script: _Script) -> None:
        self._script = script

    @classmethod
    def from_url(cls, url: str, **kwargs: object) -> "_FakeRedis":
        # 소켓 타임아웃 등 연결 옵션은 이 가짜의 관심사가 아니다(실제 소켓이 없다).
        # 옵션이 늘 때마다 시그니처를 좇지 않도록 통째로 받는다 — 타임아웃이 실제로
        # 거는지는 test_redis_socket_timeout.py가 실소켓으로 검증한다.
        cls.connect_count += 1
        return cls(cls.scripts.pop(0))

    async def ping(self) -> bool:
        if self._script.ping_fail:
            raise ConnectionError("ping failed")
        return True

    def pubsub(self) -> _FakePubSub:
        return _FakePubSub(self._script, type(self).stop_event)

    async def aclose(self) -> None:
        pass


def _envelope_msg(channel: str, uid, payload: str) -> dict[str, Any]:
    return {
        "type": "message",
        "channel": channel,
        "data": json.dumps({"target_user_ids": [str(uid)], "payload": payload}),
    }


def _setup(monkeypatch, scripts: list[_Script]) -> asyncio.Event:
    stop_event = asyncio.Event()
    _FakeRedis.scripts = scripts
    _FakeRedis.stop_event = stop_event
    _FakeRedis.connect_count = 0
    monkeypatch.setattr(pubsub_mod, "Redis", _FakeRedis)
    monkeypatch.setattr(pubsub_mod, "_RECONNECT_BACKOFF_INITIAL_SEC", 0.01)
    return stop_event


async def test_listener_reconnects_after_connect_failure(monkeypatch):
    uid = uuid4()
    stop_event = _setup(
        monkeypatch,
        [
            _Script(ping_fail=True),
            _Script(messages=[_envelope_msg("ch", uid, "after-reconnect")], stop_after=True),
        ],
    )
    received: list[tuple[Any, str]] = []

    async def handler(user_id, payload):
        received.append((user_id, payload))

    await asyncio.wait_for(
        run_user_fanout_listener(
            redis_url="redis://test", handlers={"ch": handler}, stop_event=stop_event
        ),
        timeout=5.0,
    )
    assert _FakeRedis.connect_count == 2
    assert received == [(uid, "after-reconnect")]


async def test_listener_reconnects_after_receive_error(monkeypatch):
    uid = uuid4()
    stop_event = _setup(
        monkeypatch,
        [
            _Script(get_message_error=True),  # 구독 성공 후 수신 계층에서 연결 유실
            _Script(messages=[_envelope_msg("ch", uid, "recovered")], stop_after=True),
        ],
    )
    received: list[str] = []

    async def handler(user_id, payload):
        received.append(payload)

    await asyncio.wait_for(
        run_user_fanout_listener(
            redis_url="redis://test", handlers={"ch": handler}, stop_event=stop_event
        ),
        timeout=5.0,
    )
    assert _FakeRedis.connect_count == 2
    assert received == ["recovered"]


async def test_listener_stops_promptly_during_backoff(monkeypatch):
    stop_event = _setup(monkeypatch, [_Script(ping_fail=True) for _ in range(50)])
    # 백오프 대기를 길게 만들어 stop_event가 대기를 끊는지 확인
    monkeypatch.setattr(pubsub_mod, "_RECONNECT_BACKOFF_INITIAL_SEC", 60.0)

    async def _stop_soon():
        await asyncio.sleep(0.05)
        stop_event.set()

    async def handler(user_id, payload):
        pass

    stopper = asyncio.ensure_future(_stop_soon())
    await asyncio.wait_for(
        run_user_fanout_listener(
            redis_url="redis://test", handlers={"ch": handler}, stop_event=stop_event
        ),
        timeout=5.0,
    )
    await stopper
    assert _FakeRedis.connect_count == 1  # 백오프 중 종료 — 추가 재연결 없음


async def test_backoff_reset_requires_sustained_uptime(monkeypatch):
    """구독(또는 첫 폴)만 통과하고 곧 죽는 플래핑 연결이 백오프를 리셋하지 못하게 —
    on_healthy는 연결이 _HEALTHY_UPTIME_SEC 이상 생존한 뒤에만 호출된다."""
    healthy: list[int] = []

    # 구독 성공 직후 수신 계층에서 즉사: on_healthy 미호출
    stop_event = _setup(monkeypatch, [_Script(get_message_error=True)])
    with pytest.raises(ConnectionError):
        await pubsub_mod._listen_once(
            redis_url="redis://test",
            handlers={"ch": _noop_handler},
            stop_event=stop_event,
            on_healthy=lambda: healthy.append(1),
        )
    assert healthy == []

    # 폴은 성공하지만 생존 시간이 기준(기본 5s) 미달: 여전히 미호출
    stop_event = _setup(monkeypatch, [_Script(stop_after=True)])
    await pubsub_mod._listen_once(
        redis_url="redis://test",
        handlers={"ch": _noop_handler},
        stop_event=stop_event,
        on_healthy=lambda: healthy.append(1),
    )
    assert healthy == []

    # 기준 이상 생존하면 호출
    monkeypatch.setattr(pubsub_mod, "_HEALTHY_UPTIME_SEC", 0.0)
    stop_event = _setup(monkeypatch, [_Script(stop_after=True)])
    await pubsub_mod._listen_once(
        redis_url="redis://test",
        handlers={"ch": _noop_handler},
        stop_event=stop_event,
        on_healthy=lambda: healthy.append(1),
    )
    assert healthy == [1]


async def _noop_handler(user_id, payload):
    pass


async def test_handler_error_does_not_drop_connection(monkeypatch):
    uid = uuid4()
    stop_event = _setup(
        monkeypatch,
        [
            _Script(
                messages=[
                    _envelope_msg("ch", uid, "boom"),
                    _envelope_msg("ch", uid, "ok"),
                ],
                stop_after=True,
            ),
        ],
    )
    received: list[str] = []

    async def handler(user_id, payload):
        if payload == "boom":
            raise RuntimeError("handler bug")
        received.append(payload)

    await asyncio.wait_for(
        run_user_fanout_listener(
            redis_url="redis://test", handlers={"ch": handler}, stop_event=stop_event
        ),
        timeout=5.0,
    )
    assert _FakeRedis.connect_count == 1  # 핸들러 예외로 연결을 버리지 않는다
    assert received == ["ok"]


async def test_watchdog_raises_when_subscribed_socket_goes_silent(monkeypatch):
    """구독은 됐는데 그 뒤로 **아무것도 안 오는** 연결은 유한 시간 안에 예외로 끝나야 한다.

    redis-py의 `get_message(timeout=…)`는 명시 타임아웃이 소켓 타임아웃을 덮어쓰고 None을
    반환하므로, 연결 성립 후 먹통(페일오버·SG 변경)이 되면 소켓 타임아웃으로는 예외가 영영
    안 난다. 그래서 리스너가 스스로 PING을 보내고 pong이 없으면 끊는다 — 그 예외가 있어야
    바깥의 백오프 재연결이 돈다.
    """
    # 폴은 매번 None, stop_event는 아무도 안 세운다 — 워치독만이 이 루프를 끝낼 수 있다.
    stop_event = _setup(monkeypatch, [_Script()])
    monkeypatch.setattr(pubsub_mod, "_MESSAGE_POLL_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(pubsub_mod, "_WATCHDOG_IDLE_SEC", 0.05)
    monkeypatch.setattr(pubsub_mod, "_WATCHDOG_PONG_SEC", 0.05)

    task = asyncio.ensure_future(
        pubsub_mod._listen_once(
            redis_url="redis://test",
            handlers={"ch": _noop_handler},
            stop_event=stop_event,
            on_healthy=lambda: None,
        )
    )
    done, _ = await asyncio.wait({task}, timeout=3.0)
    if task not in done:
        task.cancel()
        pytest.fail("구독 소켓이 조용해졌는데 리스너가 끝나지 않는다 — 재연결이 영영 없다")
    with pytest.raises(ConnectionError):
        task.result()
