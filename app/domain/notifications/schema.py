# 알림 와이어 형태의 집: API 스키마(BaseSchema, camelCase 직렬화) + 실시간/SNS 페이로드.
# 순수 값·빌더만 둔다 — service·worker 양쪽이 여기만 의존해 순환 import가 생기지 않는다.

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from app.common import OptionalPublicId, PublicId
from app.common.enums import NotificationKind
from app.common.schemas import BaseSchema
from app.core.ids import uuid_to_base62


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    """알림 1건의 발행 단위 — DB 행과 실시간·SNS 페이로드가 전부 이 값에서 나온다.

    생성은 NotificationService.record()가 담당해 DB 행과 이벤트가 같은 값에서
    나오게 한다(드리프트 불가).
    """

    recipient_user_id: UUID
    notification_id: UUID
    kind: NotificationKind
    actor_id: UUID | None
    post_id: UUID | None
    comment_id: UUID | None


# SNS 요약 문구 — kind에 대응 문구가 없으면 kind 값 그대로.
_SNS_SUMMARY: dict[NotificationKind, str] = {
    NotificationKind.COMMENT_ON_POST: "회원님의 게시글에 댓글이 달렸습니다.",
    NotificationKind.LIKE_POST: "회원님의 게시글에 좋아요가 눌렸습니다.",
    NotificationKind.LIKE_COMMENT: "회원님의 댓글에 좋아요가 눌렸습니다.",
}


def build_realtime_payload(event: NotificationEvent) -> dict[str, Any]:
    """SSE `data:` JSON. 필드명은 프론트 camelCase 관례에 맞춤."""

    return {
        "notificationId": uuid_to_base62(event.notification_id),
        "kind": event.kind.value,
        "actorId": None if event.actor_id is None else uuid_to_base62(event.actor_id),
        "postId": None if event.post_id is None else uuid_to_base62(event.post_id),
        "commentId": None if event.comment_id is None else uuid_to_base62(event.comment_id),
    }


def build_sns_payload(event: NotificationEvent) -> dict[str, Any]:
    """SNS `Message`에 실을 JSON 직렬화용 페이로드(구독자·Lambda에서 파싱)."""

    return {
        **build_realtime_payload(event),
        "recipientUserId": uuid_to_base62(event.recipient_user_id),
        "message": _SNS_SUMMARY.get(event.kind, event.kind.value),
    }


class NotificationItem(BaseSchema):
    id: PublicId
    kind: NotificationKind
    actor_id: OptionalPublicId = None
    post_id: OptionalPublicId = None
    comment_id: OptionalPublicId = None
    read_at: datetime | None = None
    created_at: datetime


class MarkNotificationsReadRequest(BaseSchema):
    """비어 있으면 해당 유저의 미읽음 전체를 읽음 처리."""

    ids: list[PublicId] = Field(default_factory=list)


class MarkNotificationsReadData(BaseSchema):
    updated_count: int
