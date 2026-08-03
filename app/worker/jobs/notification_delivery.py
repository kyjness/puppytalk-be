# 알림 SNS 배송 Job: DB 행 검증 → SNS publish → 성공 후 멱등 마킹. Celery 태스크는 tasks/ 에서 호출.

import json
import logging
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import NotificationKind
from app.core.config import settings
from app.core.ids import parse_public_id_value
from app.db import get_connection
from app.domain.notifications.model import Notification
from app.domain.notifications.schema import NotificationEvent, build_sns_payload
from app.infra.redis import RedisLike
from app.infra.sns import deliver_once

log = logging.getLogger(__name__)

# 워커 프로세스당 Redis 클라이언트 1개 재사용 — async_bridge가 프로세스당 단일 이벤트 루프를
# 유지하므로 안전하다(태스크마다 from_url→aclose는 커넥션 churn).
_redis_client: RedisLike | None = None


class NotificationDeliverySkip(Exception):
    """재시도 불필요(미존재·멱등 스킵·SNS 비활성)."""


def _get_redis() -> RedisLike | None:
    global _redis_client
    if _redis_client is None and settings.REDIS_URL:
        _redis_client = Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            max_connections=4,
        )
    return _redis_client


async def _load_notification(
    db: AsyncSession,
    *,
    notification_id: UUID,
    user_id: UUID,
) -> Notification:
    row = (
        await db.execute(
            select(Notification).where(
                Notification.id == notification_id,
                Notification.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotificationDeliverySkip("notification not found")
    return row


async def deliver_notification_sns_async(
    *,
    notification_id: str,
    user_id: str,
    idempotency_key: str,
) -> dict[str, str]:
    """알림 행을 DB에서 재검증 후 SNS로 발행한다(오프라인 푸시·다운스트림 구독).

    페이로드는 태스크 인자가 아니라 DB 행에서 구성한다 — 재시도 시점에도 진실은 DB.
    멱등 검사→publish→성공 후 마킹 순서는 deliver_once(인라인 폴백과 공유)가 보장한다 —
    경쟁 중복 publish가 가능하지만(at-least-once) 실패 재시도 유실보다 중복이 낫다는 선택.
    """
    if not settings.SNS_TOPIC_ARN:
        raise NotificationDeliverySkip("sns topic not configured")
    nid = parse_public_id_value(notification_id)
    uid = parse_public_id_value(user_id)
    redis = _get_redis()

    async with get_connection() as db:
        async with db.begin():
            row = await _load_notification(db, notification_id=nid, user_id=uid)

    payload = build_sns_payload(
        NotificationEvent(
            recipient_user_id=uid,
            notification_id=row.id,
            kind=NotificationKind(row.kind),
            actor_id=row.actor_id,
            post_id=row.post_id,
            comment_id=row.comment_id,
        )
    )
    delivered = await deliver_once(
        redis,
        idempotency_key,
        settings.SNS_TOPIC_ARN,
        json.dumps(payload, ensure_ascii=False),
        settings.CELERY_TASK_IDEMPOTENCY_TTL_SECONDS,
    )
    if not delivered:
        log.info("notification_delivery_skip_idempotent key=%s", idempotency_key)
        return {"status": "skipped", "reason": "idempotent"}
    log.info("notification_sns_delivered notification_id=%s user_id=%s", nid, uid)
    return {"status": "delivered", "notification_id": str(nid)}
