"""chat 스코프(#16·#19)·notifications keyset(ADR 0002) — DB 없이 검증하는 회귀 테스트."""

import inspect
import uuid

from app.common.enums import NotificationKind
from app.common.schemas import CursorPage
from app.domain.chat.service import ChatService
from app.domain.notifications.model import Notification, NotificationsRepository
from app.domain.notifications.schema import NotificationEvent, build_realtime_payload


def test_list_recent_rooms_scopes_subqueries_to_my_rooms():
    # unread·last_msg 두 서브쿼리가 내 방으로 스코프되어야 한다(#16 전역 테이블 스캔 회귀 방지).
    src = inspect.getsource(ChatService.list_recent_rooms)
    assert src.count("ChatMessage.room_id.in_(my_room_ids)") == 2


def test_is_room_member_helper_removed():
    # 세 메서드가 공유하던 _is_room_member는 인라인 판정으로 대체되어 사라졌다(#19 정리).
    assert not hasattr(ChatService, "_is_room_member")


def test_get_room_peer_info_merges_membership_into_query():
    # bare 멤버십 조회 없이 projection WHERE에 멤버십을 접어넣었는지(#19 이중 조회 제거).
    src = inspect.getsource(ChatService.get_room_peer_info)
    assert "or_(ChatRoom.user1_id == user_id, ChatRoom.user2_id == user_id)" in src
    assert "scalar_one_or_none" not in src


def test_notification_realtime_payload_is_camelcase():
    p = build_realtime_payload(
        NotificationEvent(
            recipient_user_id=uuid.uuid4(),
            notification_id=uuid.uuid4(),
            kind=NotificationKind.LIKE_POST,
            actor_id=uuid.uuid4(),
            post_id=None,
            comment_id=None,
        )
    )
    assert set(p) == {"notificationId", "kind", "actorId", "postId", "commentId"}
    assert p["postId"] is None and p["commentId"] is None
    assert isinstance(p["notificationId"], str)  # Base62 공개 id


def test_notifications_list_is_keyset_not_offset():
    # offset+count(*) → comments와 동형의 id 단일 컬럼 keyset(ADR 0002). 커서 행을 조회하지
    # 않으므로 보관정책 삭제로 커서가 사라져도 400을 내지 않는다.
    src = inspect.getsource(NotificationsRepository.list_for_user)
    assert "order_by(Notification.id.desc())" in src
    assert "Notification.id < cursor_id" in src
    assert "offset" not in src
    assert "func.count" not in src
    assert "InvalidRequestException" not in src


def test_cursor_page_has_no_total():
    # ADR 0002: 커서 페이지는 total을 노출하지 않는다.
    assert "total" not in CursorPage.model_fields


def test_notification_indexes_reconciled():
    table = Notification.metadata.tables["notifications"]
    names = {i.name for i in table.indexes}
    # 정렬 축(id DESC)에 맞는 인덱스 + 전체 읽음용 부분 인덱스.
    assert "ix_notifications_user_recent" in names
    assert "ix_notifications_user_unread" in names
    # 인라인 index=True 단독 user_id·created_at 정렬 인덱스는 대체됐다.
    assert "ix_notifications_user_id" not in names
    assert "ix_notifications_user_created" not in names
