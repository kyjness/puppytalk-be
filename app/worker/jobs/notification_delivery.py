# 알림 SNS 배송 arq 잡: DB 행 검증 → SNS publish → 성공 후 멱등 마킹 + 재시도 정책.

import json
import logging
import random
from uuid import UUID

from arq.worker import Retry
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import NotificationKind
from app.core.config import settings
from app.core.ids import parse_public_id_value
from app.db import get_connection
from app.domain.notifications.model import Notification
from app.domain.notifications.schema import NotificationEvent, build_sns_payload
from app.infra.redis import RedisLike, create_redis_client
from app.infra.sns import deliver_once

log = logging.getLogger(__name__)

# 워커 프로세스당 Redis 클라이언트 1개 재사용 — arq 워커는 프로세스당 단일 이벤트 루프에서
# 잡을 돌리므로 안전하다(잡마다 from_url→aclose는 커넥션 churn).
_redis_client: RedisLike | None = None


class NotificationDeliverySkip(Exception):
    """재시도 불필요(미존재·멱등 스킵·SNS 비활성)."""


def _get_redis() -> RedisLike | None:
    global _redis_client
    if _redis_client is None:
        # 앱과 같은 생성부를 쓴다 — 타임아웃 없이 두면 먹통 Redis에서 멱등성 조회가
        # 반환하지 않아 워커 슬롯이 소진된다(ADR 0005). 풀만 작게(태스크는 순차).
        _redis_client = create_redis_client(max_connections=4)
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
        settings.WORKER_IDEMPOTENCY_TTL_SECONDS,
    )
    if not delivered:
        log.info("notification_delivery_skip_idempotent key=%s", idempotency_key)
        return {"status": "skipped", "reason": "idempotent"}
    log.info("notification_sns_delivered notification_id=%s user_id=%s", nid, uid)
    return {"status": "delivered", "notification_id": str(nid)}


def _retry_delay(job_try: int) -> float:
    """지수 백오프 + 지터. Celery의 `retry_backoff`·`retry_jitter`와 같은 곡선.

    지터가 없으면 한 번에 실패한 배송들이 같은 초에 재시도돼 SNS로 몰린다(thundering herd).
    """
    base = settings.ARQ_RETRY_BASE_SECONDS * (2 ** (job_try - 1))
    return base * random.uniform(1.0, 1.5)


async def deliver_notification_sns(
    ctx: dict,
    *,
    notification_id: str,
    user_id: str,
    idempotency_key: str,
) -> dict[str, str]:
    """워커가 실행하는 진입점 — 위 잡 본문에 재시도 정책만 얹는다.

    **arq는 그냥 예외를 올리면 재시도하지 않는다** — `Retry`를 올려야 한다. Celery의
    `self.retry(exc=...)`와 시맨틱이 반대라, 이 교체에서 가장 놓치기 쉬운 지점이다
    (ADR 0020 결정 2). 총 시도 횟수는 `WorkerSettings.max_tries`가 막는다.

    잡이 둘 이상이 되면 이 번역을 공용 데코레이터로 올린다 — 지금은 잡이 하나라
    미리 만들지 않는다(ADR 0020 "일부러 하지 않은 것").
    """
    try:
        return await deliver_notification_sns_async(
            notification_id=notification_id,
            user_id=user_id,
            idempotency_key=idempotency_key,
        )
    except NotificationDeliverySkip as e:
        # 재시도해도 결과가 같다(미존재·멱등 스킵·SNS 비활성) — Retry를 올리지 않는다.
        log.warning("deliver_notification_sns_skip: %s", e)
        return {"status": "skipped", "reason": str(e)}
    except Exception:
        job_try = int(ctx.get("job_try", 1))
        log.exception(
            "deliver_notification_sns_failed notification_id=%s try=%s",
            notification_id,
            job_try,
        )
        raise Retry(defer=_retry_delay(job_try)) from None
