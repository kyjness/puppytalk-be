# 알림 애플리케이션 서비스: PostgreSQL 영속화, 커밋 이후 Redis Pub/Sub, SSE 구독 스트림.

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from typing import Any, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.common.enums import NotificationKind
from app.common.exceptions import TooManyRequestsException
from app.core.config import settings
from app.core.ids import uuid_to_base62
from app.domain.notifications.model import Notification, NotificationsRepository
from app.domain.notifications.schema import (
    NotificationEvent,
    NotificationItem,
    build_realtime_payload,
    build_sns_payload,
)
from app.domain.notifications.stream import NOTIF_SSE_FANOUT_CHANNEL, notification_sse_manager
from app.infra.pubsub import publish_user_envelope
from app.infra.redis import RedisLike
from app.infra.sns import deliver_once

log = logging.getLogger(__name__)

# fire-and-forget SNS 태스크의 강참조 보관 — 이벤트 루프는 태스크를 약참조하므로
# 참조를 안 잡아두면 완료 전 GC로 조용히 사라질 수 있다.
_sns_inline_tasks: set[asyncio.Task[None]] = set()


async def drain_sns_inline_tasks(timeout_seconds: float = 5.0) -> None:
    """lifespan 셧다운용: 진행 중인 인라인 SNS 태스크를 짧게 기다린다.

    publish와 멱등 마킹 사이에서 프로세스가 끊기면 미마킹으로 남아 워커 재시도 시
    이중 배송 창이 다시 열린다 — close_redis 전에 호출해 창을 닫는다. 시간 내 못
    끝나면 취소(fail-open — 인앱 전달·DB 행은 이미 확보된 상태)."""
    if not _sns_inline_tasks:
        return
    tasks = tuple(_sns_inline_tasks)
    _, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
    for task in pending:
        task.cancel()


def _sns_idempotency_key(notification_id: UUID) -> str:
    """결정적 멱등키 — Celery enqueue와 인라인 폴백이 같은 키를 써서 이중 배송 창을 닫는다."""
    return f"sns:{uuid_to_base62(notification_id)}"


class NotificationService:
    """수신자별 알림 레코드와 실시간 전달을 조율. Publish는 항상 트랜잭션 커밋 이후 호출."""

    @classmethod
    async def record(
        cls,
        *,
        recipient_user_id: UUID,
        kind: NotificationKind,
        actor_id: UUID | None,
        post_id: UUID | None,
        comment_id: UUID | None,
        db: AsyncSession,
    ) -> NotificationEvent:
        """트랜잭션 안에서 알림 행을 영속화하고, 커밋 후 publish_after_commit에 넘길
        이벤트를 돌려준다 — 생산자가 같은 필드를 두 번 조립할 일이 없다."""
        nid = await NotificationsRepository.insert(
            user_id=recipient_user_id,
            kind=kind,
            actor_id=actor_id,
            post_id=post_id,
            comment_id=comment_id,
            db=db,
        )
        return NotificationEvent(
            recipient_user_id=recipient_user_id,
            notification_id=nid,
            kind=kind,
            actor_id=actor_id,
            post_id=post_id,
            comment_id=comment_id,
        )

    @classmethod
    async def _dispatch_sns_publish(cls, redis: RedisLike | None, event: NotificationEvent) -> None:
        """오프라인 배송(SNS)은 재시도·백오프가 필요한 외부 I/O라 Celery로 오프로드한다.

        워커 비활성(CELERY_ENABLED=false)·브로커 장애 시에는 인라인 fire-and-forget으로
        폴백한다(fail-open — 실시간 인앱 경로와 DB는 이미 확보된 상태).
        """
        if not settings.SNS_TOPIC_ARN:
            return
        if settings.CELERY_ENABLED:
            try:
                from app.worker.tasks.notifications import deliver_notification_sns

                # 결정적 멱등키: 같은 알림의 중복 enqueue가 워커에서 1회 배송으로 수렴.
                await run_in_threadpool(
                    cast(Any, deliver_notification_sns).delay,
                    notification_id=uuid_to_base62(event.notification_id),
                    user_id=uuid_to_base62(event.recipient_user_id),
                    idempotency_key=_sns_idempotency_key(event.notification_id),
                )
                return
            except Exception:
                log.exception(
                    "알림 SNS Celery enqueue 실패 — 인라인 폴백. notification_id=%s",
                    event.notification_id,
                )
        cls._schedule_sns_publish(redis, event)

    @classmethod
    def _schedule_sns_publish(cls, redis: RedisLike | None, event: NotificationEvent) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        task = loop.create_task(cls._publish_sns_task(redis, event))
        _sns_inline_tasks.add(task)
        task.add_done_callback(_sns_inline_tasks.discard)

    @classmethod
    async def _publish_sns_task(cls, redis: RedisLike | None, event: NotificationEvent) -> None:
        topic = settings.SNS_TOPIC_ARN
        message_json = json.dumps(build_sns_payload(event), ensure_ascii=False)
        try:
            # 워커 잡과 같은 멱등 스토어·키·안무(deliver_once) — 브로커 ack 유실로
            # enqueue와 인라인 폴백이 둘 다 실행돼도(교차 경로) 한쪽만 배송된다.
            await deliver_once(
                redis,
                _sns_idempotency_key(event.notification_id),
                topic,
                message_json,
                settings.CELERY_TASK_IDEMPOTENCY_TTL_SECONDS,
            )
        except Exception:
            log.exception(
                "알림 SNS publish 실패(인앱·DB는 유지). recipient=%s topic=%s",
                event.recipient_user_id,
                topic,
            )

    @classmethod
    async def publish_after_commit(cls, redis: RedisLike | None, event: NotificationEvent) -> None:
        """트랜잭션이 성공적으로 커밋된 뒤에만 호출. Redis 장애 시 DB 데이터는 유지(fail-open)."""

        payload_json = json.dumps(build_realtime_payload(event), ensure_ascii=False)
        # 같은 인스턴스의 SSE 스트림은 먼저 직접 전달 — Redis·구독 리스너 상태에 의존하지
        # 않는다. 크로스 인스턴스는 단일 채널 envelope publish(chat DM과 동형) — 리스너가
        # origin 비교로 자기 발행분을 건너뛰어 중복 없음. publish 실패 시 다른 인스턴스
        # 수신자는 GET /notifications로 동기화 가능하다(at-most-once).
        await notification_sse_manager.deliver(event.recipient_user_id, payload_json)
        # 크로스 인스턴스 publish(Redis)와 SNS 오프로드(브로커)는 데이터·순서 의존이 없고
        # 각자 예외를 삼키므로 병렬로 — 요청 지연이 합이 아니라 max가 된다.
        await asyncio.gather(
            publish_user_envelope(
                redis,
                NOTIF_SSE_FANOUT_CHANNEL,
                target_user_ids=[event.recipient_user_id],
                payload=payload_json,
            ),
            cls._dispatch_sns_publish(redis, event),
        )

    @staticmethod
    def row_to_item(row: Notification) -> NotificationItem:
        return NotificationItem(
            id=row.id,
            kind=NotificationKind(row.kind),
            actor_id=row.actor_id,
            post_id=row.post_id,
            comment_id=row.comment_id,
            read_at=row.read_at,
            created_at=row.created_at,
        )

    @classmethod
    async def list_notifications(
        cls,
        user_id: UUID,
        *,
        cursor_id: UUID | None,
        size: int,
        db: AsyncSession,
    ) -> tuple[list[NotificationItem], bool]:
        async with db.begin():
            rows, has_more = await NotificationsRepository.list_for_user(
                user_id, cursor_id=cursor_id, size=size, db=db
            )
        return [cls.row_to_item(r) for r in rows], has_more

    @classmethod
    async def mark_read(
        cls,
        user_id: UUID,
        *,
        ids: list[UUID],
        db: AsyncSession,
    ) -> int:
        async with db.begin():
            return await NotificationsRepository.mark_read(user_id, notification_ids=ids, db=db)

    @classmethod
    async def purge_old_notifications(
        cls,
        *,
        older_than_days: int,
        chunk_size: int,
        db: AsyncSession,
    ) -> int:
        """created_at 기준 보관기간 초과 알림 삭제(청크 반복). 정책 숫자는 호출부(main)가 소유."""
        total = 0
        # 단일 트랜잭션에 너무 많이 태우면 락/부하가 커질 수 있어, 청크별 begin()으로 끊는다
        # (users 퍼지와 동형).
        while True:
            async with db.begin():
                deleted = await NotificationsRepository.delete_older_than(
                    older_than_days=older_than_days, limit=chunk_size, db=db
                )
            if not deleted:
                break
            total += deleted
        return total

    @staticmethod
    async def sse_register(user_id: UUID) -> asyncio.Queue[str]:
        """스트림 시작 **전에** 큐를 잡는다 — 상한 초과를 429로 돌려주려면 등록이 응답
        본문 시작보다 앞서야 한다(제너레이터 안에서 거절하면 이미 200이 나간 뒤다)."""
        queue = await notification_sse_manager.register(user_id)
        if queue is None:
            raise TooManyRequestsException(
                message="동시 실시간 연결 수를 초과했습니다. 사용하지 않는 탭을 닫고 다시 시도해주세요."
            )
        return queue

    @staticmethod
    async def sse_subscribe(
        user_id: UUID,
        queue: asyncio.Queue[str],
        *,
        heartbeat_interval_sec: float = 25.0,
    ) -> AsyncGenerator[str]:
        """로컬 팬아웃 큐 대기 — 연결마다 Redis pubsub을 점유하지 않는다(공유 풀 고갈 방지).
        클라이언트 연결 해제 시 제너레이터 취소 → 큐 등록 해제."""
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=heartbeat_interval_sec)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"data: {payload}\n\n"
        finally:
            await notification_sse_manager.unregister(user_id, queue)
