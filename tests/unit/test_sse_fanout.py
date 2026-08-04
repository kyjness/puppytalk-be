"""알림 SSE 팬아웃(chat 동형) 단위 테스트.

핵심 불변식: SSE 연결은 Redis pubsub을 점유하지 않고(공유 풀 고갈 방지) 로컬 큐로 대기하며,
발행은 단일 채널 envelope → 공용 리스너가 채널별 핸들러로 디스패치, publish 실패 시
같은 인스턴스 수신자는 로컬로 폴백 전달된다.
"""

import asyncio
import json
from typing import Any
from uuid import uuid4

import pytest
from app.domain.chat.service import ChatService
from app.domain.notifications.schema import NotificationEvent
from app.domain.notifications.service import NotificationService
from app.domain.notifications.stream import (
    NOTIF_SSE_FANOUT_CHANNEL,
    SseFanoutManager,
    notification_sse_manager,
)
from app.infra import pubsub as pubsub_mod
from app.infra.pubsub import publish_user_envelope, run_user_fanout_listener

from tests.unit.fakes import FakeRedis

pytestmark = pytest.mark.asyncio


# --- SseFanoutManager ---


async def test_manager_delivers_to_all_user_queues():
    manager = SseFanoutManager()
    uid = uuid4()
    q1 = await manager.register(uid)
    q2 = await manager.register(uid)
    await manager.deliver(uid, "hello")
    assert q1.get_nowait() == "hello"
    assert q2.get_nowait() == "hello"


async def test_manager_unregister_removes_empty_bucket():
    manager = SseFanoutManager()
    uid = uuid4()
    queue = await manager.register(uid)
    await manager.unregister(uid, queue)
    await manager.deliver(uid, "dropped")  # 등록 없음 → no-op
    assert manager._by_user == {}


async def test_manager_drops_when_queue_full():
    manager = SseFanoutManager()
    uid = uuid4()
    queue = await manager.register(uid)
    for i in range(queue.maxsize):
        await manager.deliver(uid, str(i))
    await manager.deliver(uid, "overflow")  # 예외 없이 드롭
    assert queue.qsize() == queue.maxsize


# --- sse_subscribe ---


async def test_sse_subscribe_yields_delivered_payload_and_unregisters():
    uid = uuid4()
    stream = NotificationService.sse_subscribe(uid, heartbeat_interval_sec=5.0)
    task = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)  # register까지 진행
    await notification_sse_manager.deliver(uid, '{"k":1}')
    assert await task == 'data: {"k":1}\n\n'
    await stream.aclose()
    assert uid not in notification_sse_manager._by_user


async def test_sse_subscribe_emits_ping_on_idle():
    uid = uuid4()
    stream = NotificationService.sse_subscribe(uid, heartbeat_interval_sec=0.01)
    try:
        assert await anext(stream) == ": ping\n\n"
    finally:
        await stream.aclose()


# --- publish_after_commit 팬아웃 경로 ---


def _event(uid) -> NotificationEvent:
    from app.common.enums import NotificationKind

    return NotificationEvent(
        recipient_user_id=uid,
        notification_id=uuid4(),
        kind=NotificationKind.LIKE_POST,
        actor_id=None,
        post_id=None,
        comment_id=None,
    )


async def test_publish_after_commit_sends_single_channel_envelope_with_origin():
    uid = uuid4()
    redis = FakeRedis()
    await NotificationService.publish_after_commit(redis, _event(uid))  # type: ignore[arg-type]
    [(channel, raw)] = redis.published
    assert channel == NOTIF_SSE_FANOUT_CHANNEL
    env = json.loads(raw)
    assert env["target_user_ids"] == [str(uid)]
    # 구포맷 스칼라 병기는 롤링 창 종료로 제거됐다 — wire에 다시 스며들지 않게 고정.
    assert "target_user_id" not in env
    assert env["origin"] == pubsub_mod._instance_id()  # 리스너의 자기 발행분 스킵 근거
    assert json.loads(env["payload"])["kind"] == "LIKE_POST"


async def test_publish_after_commit_delivers_locally_even_when_publish_succeeds():
    """로컬 전달은 publish·리스너 상태에 의존하지 않는다 — 항상 직접 전달."""
    uid = uuid4()
    queue = await notification_sse_manager.register(uid)
    try:
        await NotificationService.publish_after_commit(
            FakeRedis(),  # type: ignore[arg-type]
            _event(uid),
        )
        assert queue.qsize() == 1
    finally:
        await notification_sse_manager.unregister(uid, queue)


async def test_publish_after_commit_delivers_locally_when_publish_fails():
    uid = uuid4()
    queue = await notification_sse_manager.register(uid)
    try:
        await NotificationService.publish_after_commit(
            FakeRedis(fail_publish=True),  # type: ignore[arg-type]
            _event(uid),
        )
        payload = queue.get_nowait()
        assert json.loads(payload)["kind"] == "LIKE_POST"
    finally:
        await notification_sse_manager.unregister(uid, queue)


async def test_publish_after_commit_delivers_locally_without_redis():
    uid = uuid4()
    queue = await notification_sse_manager.register(uid)
    try:
        await NotificationService.publish_after_commit(None, _event(uid))
        assert queue.qsize() == 1
    finally:
        await notification_sse_manager.unregister(uid, queue)


# --- chat 팬아웃 (로컬 우선 + envelope publish) ---


async def test_chat_fanout_sends_locally_regardless_of_publish_result(monkeypatch):
    sent: list[tuple[Any, str]] = []

    async def fake_send(user_id, message):
        sent.append((user_id, message))

    monkeypatch.setattr(
        "app.domain.chat.service.chat_connection_manager.send_personal_message", fake_send
    )
    peer, sender = uuid4(), uuid4()
    await ChatService._fanout_dm(
        FakeRedis(fail_publish=True), peer_id=peer, sender_id=sender, wire="w"
    )  # type: ignore[arg-type]
    assert [(peer, "w"), (sender, "w")] == sent

    sent.clear()
    ok_redis = FakeRedis()
    await ChatService._fanout_dm(ok_redis, peer_id=peer, sender_id=sender, wire="w")  # type: ignore[arg-type]
    assert [(peer, "w"), (sender, "w")] == sent  # publish 성공이어도 로컬은 직접 전달
    # 같은 wire의 peer·sender는 envelope 1건에 수신자 목록으로 — 건별 발행은 RTT·파싱 2배.
    [(_, raw)] = ok_redis.published
    assert json.loads(raw)["target_user_ids"] == [str(peer), str(sender)]


async def test_publish_user_envelope_returns_false_without_redis():
    assert await publish_user_envelope(None, "ch", target_user_ids=[uuid4()], payload="p") is False


async def test_instance_id_regenerates_per_process(monkeypatch):
    """preload-then-fork(gunicorn --preload)에서 전 워커가 같은 origin을 물려받으면
    형제 워커 envelope가 전부 자기 발행분으로 오인·유실된다 — pid가 바뀌면 재생성."""
    first = pubsub_mod._instance_id()
    assert pubsub_mod._instance_id() == first  # 같은 프로세스에서는 안정
    monkeypatch.setattr(pubsub_mod.os, "getpid", lambda: -12345)  # fork된 자식 흉내
    assert pubsub_mod._instance_id() != first


# --- 공용 리스너 채널 디스패치 ---


class _FakePubSub:
    def __init__(self, messages: list[dict[str, Any]], stop_event: asyncio.Event) -> None:
        self._messages = messages
        self._stop_event = stop_event
        self.closed = False

    async def subscribe(self, *channels: str) -> None:
        self.channels = channels

    async def unsubscribe(self, *channels: str) -> None:
        pass

    async def get_message(self, *, ignore_subscribe_messages: bool, timeout: float):
        if self._messages:
            return self._messages.pop(0)
        self._stop_event.set()
        return None

    async def aclose(self) -> None:
        self.closed = True


class _FakeListenerRedis:
    last: "_FakeListenerRedis | None" = None
    messages: list[dict[str, Any]] = []
    stop_event: asyncio.Event

    def __init__(self) -> None:
        self.pubsub_obj = _FakePubSub(list(self.messages), self.stop_event)
        type(self).last = self

    @classmethod
    def from_url(cls, url: str, decode_responses: bool = False) -> "_FakeListenerRedis":
        return cls()

    async def ping(self) -> bool:
        return True

    def pubsub(self) -> _FakePubSub:
        return self.pubsub_obj

    async def aclose(self) -> None:
        pass


async def test_listener_dispatches_by_channel(monkeypatch):
    stop_event = asyncio.Event()
    uid_chat, uid_notif = uuid4(), uuid4()
    _FakeListenerRedis.stop_event = stop_event
    uid_chat2 = uuid4()
    _FakeListenerRedis.messages = [
        {
            # 수신자 목록 envelope 1건 → 수신자별로 핸들러 호출
            "type": "message",
            "channel": "ch:chat",
            "data": json.dumps(
                {"target_user_ids": [str(uid_chat), str(uid_chat2)], "payload": "dm"}
            ),
        },
        {
            # 구포맷 스칼라 target_user_id — 롤링 창 종료로 파서가 버려야 한다(전달 0건)
            "type": "message",
            "channel": "ch:notif",
            "data": json.dumps({"target_user_id": str(uid_notif), "payload": "legacy"}),
        },
        {
            # 채널별 핸들러 라우팅 — notif 채널 목록 포맷
            "type": "message",
            "channel": "ch:notif",
            "data": json.dumps({"target_user_ids": [str(uid_notif)], "payload": "notif"}),
        },
        {
            # 자기 인스턴스 발행분 — 로컬은 발행 시 이미 직접 전달됐으므로 스킵돼야 한다
            "type": "message",
            "channel": "ch:chat",
            "data": json.dumps(
                {
                    "origin": pubsub_mod._instance_id(),
                    "target_user_ids": [str(uid_chat)],
                    "payload": "self-dup",
                }
            ),
        },
        {"type": "message", "channel": "ch:unknown", "data": "ignored"},
        {"type": "message", "channel": "ch:chat", "data": "not-json"},  # envelope invalid → skip
    ]
    monkeypatch.setattr(pubsub_mod, "Redis", _FakeListenerRedis)

    received: dict[str, list[tuple[Any, str]]] = {"chat": [], "notif": []}

    async def chat_handler(user_id, payload):
        received["chat"].append((user_id, payload))

    async def notif_handler(user_id, payload):
        received["notif"].append((user_id, payload))

    await run_user_fanout_listener(
        redis_url="redis://test",
        handlers={"ch:chat": chat_handler, "ch:notif": notif_handler},
        stop_event=stop_event,
    )
    assert received["chat"] == [(uid_chat, "dm"), (uid_chat2, "dm")]
    assert received["notif"] == [(uid_notif, "notif")]
    assert _FakeListenerRedis.last is not None
    assert _FakeListenerRedis.last.pubsub_obj.closed  # teardown 보장
