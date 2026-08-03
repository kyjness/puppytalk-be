"""알림 SNS 배송 오프로드 단위 테스트.

실 브로커/SNS 없이 몽키패치로 라우팅 불변식을 검증한다:
CELERY_ENABLED=true → 태스크 enqueue(결정적 멱등키) · enqueue 실패/비활성 → 인라인 폴백 ·
워커 잡·인라인 폴백 모두 publish 성공 후에만 같은 멱등 스토어에 마킹(교차 경로 이중 배송 차단).
"""

import asyncio
import uuid

import pytest
from app.common.enums import NotificationKind
from app.core.config import settings
from app.core.ids import uuid_to_base62
from app.domain.notifications import service as notif_service
from app.domain.notifications.schema import NotificationEvent
from app.domain.notifications.service import NotificationService
from app.infra import sns as sns_mod
from app.worker.jobs import notification_delivery as job

from tests.unit.fakes import FakeRedis

pytestmark = pytest.mark.asyncio

_KIND = NotificationKind.COMMENT_ON_POST


def _ids():
    return uuid.uuid4(), uuid.uuid4()


class _RecordingTask:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self._fail = fail

    def delay(self, **kwargs):
        if self._fail:
            raise ConnectionError("broker down")
        self.calls.append(kwargs)


def _event(recipient, nid) -> NotificationEvent:
    return NotificationEvent(
        recipient_user_id=recipient,
        notification_id=nid,
        kind=_KIND,
        actor_id=None,
        post_id=None,
        comment_id=None,
    )


async def _dispatch(recipient, nid):
    await NotificationService._dispatch_sns_publish(None, _event(recipient, nid))


def _capture_inline(monkeypatch) -> list[NotificationEvent]:
    inline_calls: list[NotificationEvent] = []
    monkeypatch.setattr(
        NotificationService,
        "_schedule_sns_publish",
        classmethod(lambda cls, redis, event: inline_calls.append(event)),
    )
    return inline_calls


async def test_dispatch_enqueues_celery_task_when_enabled(monkeypatch):
    recipient, nid = _ids()
    task = _RecordingTask()
    monkeypatch.setattr(settings, "SNS_TOPIC_ARN", "arn:aws:sns:test:topic")
    monkeypatch.setattr(settings, "CELERY_ENABLED", True)
    import app.worker.tasks.notifications as tasks_mod

    monkeypatch.setattr(tasks_mod, "deliver_notification_sns", task)
    inline_calls = _capture_inline(monkeypatch)

    await _dispatch(recipient, nid)

    assert len(task.calls) == 1
    call = task.calls[0]
    assert call["notification_id"] == uuid_to_base62(nid)
    assert call["user_id"] == uuid_to_base62(recipient)
    # 결정적 멱등키: 같은 알림의 중복 enqueue가 워커에서 1회 배송으로 수렴해야 한다.
    assert call["idempotency_key"] == f"sns:{uuid_to_base62(nid)}"
    assert inline_calls == []


async def test_dispatch_falls_back_inline_when_enqueue_fails(monkeypatch):
    recipient, nid = _ids()
    monkeypatch.setattr(settings, "SNS_TOPIC_ARN", "arn:aws:sns:test:topic")
    monkeypatch.setattr(settings, "CELERY_ENABLED", True)
    import app.worker.tasks.notifications as tasks_mod

    monkeypatch.setattr(tasks_mod, "deliver_notification_sns", _RecordingTask(fail=True))
    inline_calls = _capture_inline(monkeypatch)

    await _dispatch(recipient, nid)
    assert len(inline_calls) == 1


async def test_dispatch_uses_inline_when_celery_disabled(monkeypatch):
    recipient, nid = _ids()
    monkeypatch.setattr(settings, "SNS_TOPIC_ARN", "arn:aws:sns:test:topic")
    monkeypatch.setattr(settings, "CELERY_ENABLED", False)
    inline_calls = _capture_inline(monkeypatch)

    await _dispatch(recipient, nid)
    assert len(inline_calls) == 1


async def test_dispatch_noop_without_topic(monkeypatch):
    recipient, nid = _ids()
    monkeypatch.setattr(settings, "SNS_TOPIC_ARN", "")
    monkeypatch.setattr(settings, "CELERY_ENABLED", True)
    inline_calls = _capture_inline(monkeypatch)

    await _dispatch(recipient, nid)
    assert inline_calls == []


# ---- 공용 멱등 스토어 fake ----


class _FakeRow:
    def __init__(self, nid, uid) -> None:
        self.id = nid
        self.user_id = uid
        self.kind = _KIND.value
        self.actor_id = None
        self.post_id = None
        self.comment_id = None


def _patch_job_db(monkeypatch, row):
    from contextlib import asynccontextmanager

    class _FakeTx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeDb:
        def begin(self):
            return _FakeTx()

    @asynccontextmanager
    async def _fake_conn():
        yield _FakeDb()

    async def _fake_load(db, *, notification_id, user_id):
        return row

    monkeypatch.setattr(job, "get_connection", _fake_conn)
    monkeypatch.setattr(job, "_load_notification", _fake_load)


def _patch_publish(monkeypatch, target_mod, published: list[str], fail: bool = False):
    async def _publish(topic, msg):
        if fail:
            raise ConnectionError("sns down")
        published.append(msg)

    monkeypatch.setattr(target_mod, "publish_sns", _publish)


# ---- 워커 잡: 멱등 마킹 순서 ----


async def test_job_marks_idempotent_only_after_publish_success(monkeypatch):
    uid, nid = _ids()
    client = FakeRedis()
    monkeypatch.setattr(settings, "SNS_TOPIC_ARN", "arn:aws:sns:test:topic")
    monkeypatch.setattr(job, "_redis_client", client)
    _patch_job_db(monkeypatch, _FakeRow(nid, uid))

    published: list[str] = []
    _patch_publish(monkeypatch, sns_mod, published)

    out = await job.deliver_notification_sns_async(
        notification_id=str(nid), user_id=str(uid), idempotency_key="sns:test-key"
    )
    assert out["status"] == "delivered"
    assert len(published) == 1
    assert client.set_calls == [f"{sns_mod.DELIVERED_KEY_PREFIX}sns:test-key"]


async def test_job_does_not_mark_idempotent_when_publish_fails(monkeypatch):
    uid, nid = _ids()
    client = FakeRedis()
    monkeypatch.setattr(settings, "SNS_TOPIC_ARN", "arn:aws:sns:test:topic")
    monkeypatch.setattr(job, "_redis_client", client)
    _patch_job_db(monkeypatch, _FakeRow(nid, uid))

    published: list[str] = []
    _patch_publish(monkeypatch, sns_mod, published, fail=True)

    with pytest.raises(ConnectionError):
        await job.deliver_notification_sns_async(
            notification_id=str(nid), user_id=str(uid), idempotency_key="sns:test-key"
        )
    # 마킹이 없어야 Celery 재시도가 멱등 skip으로 유실되지 않는다.
    assert client.set_calls == []


async def test_job_skips_when_already_delivered(monkeypatch):
    uid, nid = _ids()
    client = FakeRedis(preloaded={f"{sns_mod.DELIVERED_KEY_PREFIX}sns:test-key": "1"})
    monkeypatch.setattr(settings, "SNS_TOPIC_ARN", "arn:aws:sns:test:topic")
    monkeypatch.setattr(job, "_redis_client", client)
    _patch_job_db(monkeypatch, _FakeRow(nid, uid))

    published: list[str] = []
    _patch_publish(monkeypatch, sns_mod, published)

    out = await job.deliver_notification_sns_async(
        notification_id=str(nid), user_id=str(uid), idempotency_key="sns:test-key"
    )
    assert out == {"status": "skipped", "reason": "idempotent"}
    assert published == []


# ---- 인라인 폴백: 워커와 같은 멱등 스토어 공유 ----


async def _inline_publish(redis, nid, recipient):
    await NotificationService._publish_sns_task(redis, _event(recipient, nid))


async def test_inline_fallback_skips_when_worker_already_delivered(monkeypatch):
    """브로커 ack 유실 교차 경로: 워커가 먼저 배송했으면 인라인 폴백은 publish하지 않는다."""
    recipient, nid = _ids()
    key = f"{sns_mod.DELIVERED_KEY_PREFIX}sns:{uuid_to_base62(nid)}"
    client = FakeRedis(preloaded={key: "1"})
    monkeypatch.setattr(settings, "SNS_TOPIC_ARN", "arn:aws:sns:test:topic")

    published: list[str] = []
    _patch_publish(monkeypatch, sns_mod, published)

    await _inline_publish(client, nid, recipient)
    assert published == []


async def test_inline_fallback_marks_same_store_after_publish(monkeypatch):
    """인라인 폴백도 publish 성공 후 워커와 같은 키에 마킹 — 이후 워커 재실행이 skip된다."""
    recipient, nid = _ids()
    client = FakeRedis()
    monkeypatch.setattr(settings, "SNS_TOPIC_ARN", "arn:aws:sns:test:topic")

    published: list[str] = []
    _patch_publish(monkeypatch, sns_mod, published)

    await _inline_publish(client, nid, recipient)
    assert len(published) == 1
    assert client.set_calls == [f"{sns_mod.DELIVERED_KEY_PREFIX}sns:{uuid_to_base62(nid)}"]


# ---- 인라인 태스크 셧다운 드레인 ----


async def test_drain_waits_for_inline_tasks():
    """셧다운이 인라인 태스크를 드레인해야 publish~마킹 사이 끊김(이중 배송 창)이 없다."""
    done = asyncio.Event()

    async def _slow():
        await asyncio.sleep(0.05)
        done.set()

    task = asyncio.get_running_loop().create_task(_slow())
    notif_service._sns_inline_tasks.add(task)
    task.add_done_callback(notif_service._sns_inline_tasks.discard)

    await notif_service.drain_sns_inline_tasks(timeout_seconds=1.0)
    assert done.is_set()


async def test_drain_cancels_tasks_exceeding_timeout():
    async def _hang():
        await asyncio.sleep(60)

    task = asyncio.get_running_loop().create_task(_hang())
    notif_service._sns_inline_tasks.add(task)
    task.add_done_callback(notif_service._sns_inline_tasks.discard)

    await notif_service.drain_sns_inline_tasks(timeout_seconds=0.05)
    with pytest.raises(asyncio.CancelledError):
        await task
