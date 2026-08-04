# 1:1 채팅방·메시지 ORM. user1_id < user2_id 정규화 + 복합 유니크로 동시 생성 레이스 방지.

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_uuid7
from app.db.base_class import PG_UUID, Base, utc_now


def normalize_dm_user_ids(user_a: UUID, user_b: UUID) -> tuple[UUID, UUID]:
    """채팅방 upsert 시 항상 동일한 (user1_id, user2_id) 행으로 수렴. DB CHECK·UNIQUE와 일치.

    동일 ID 쌍은 호출부(get_or_create_room)가 SelfDmException으로 먼저 거른다."""
    return (user_a, user_b) if user_a < user_b else (user_b, user_a)


class ChatRoom(Base):
    __tablename__ = "chat_rooms"
    __table_args__ = (
        UniqueConstraint("user1_id", "user2_id", name="uq_chat_rooms_user_pair"),
        CheckConstraint("user1_id < user2_id", name="ck_chat_rooms_user_order"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID, primary_key=True, default=new_uuid7)
    user1_id: Mapped[UUID] = mapped_column(
        PG_UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user2_id: Mapped[UUID] = mapped_column(
        PG_UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        Index(
            "ix_chat_messages_room_created",
            "room_id",
            text("created_at DESC"),
        ),
        # 미읽음 카운트(#16)는 방별 미읽음 소수 행만 필요하다. 부분 인덱스로 읽음 메시지를
        # 인덱스에서 배제해 방 전체 스캔 대신 미읽음만 도는 인덱스 스캔이 되게 한다.
        Index(
            "ix_chat_messages_unread",
            "room_id",
            # 술어를 쿼리의 .is_(False)(→ IS false)와 동일 형태로 맞춰 플래너가 부분 인덱스를
            # 확실히 선택하게 한다(= false 형태면 IS false 필터와 매칭 실패로 seq-scan 위험).
            postgresql_where=text("is_read IS false"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID, primary_key=True, default=new_uuid7)
    room_id: Mapped[UUID] = mapped_column(
        PG_UUID, ForeignKey("chat_rooms.id", ondelete="CASCADE"), nullable=False
    )
    sender_id: Mapped[UUID] = mapped_column(
        PG_UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
