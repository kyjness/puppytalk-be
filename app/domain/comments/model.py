# 댓글 ORM 정의. 쿼리/트랜잭션 로직은 repository.py (posts와 동형 분리).
# 관계는 쿼리가 실제 로드하는 것만 선언한다 — 대댓글 트리는 parent_id 컬럼으로 조립하므로
# parent/replies 관계 없음, 좋아요는 CommentLike 컬럼 직접 쿼리라 관계 없음.

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.ids import new_uuid7
from app.db.base_class import PG_UUID, Base
from app.domain.users.model import User


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[UUID] = mapped_column(PG_UUID, primary_key=True, default=new_uuid7)
    post_id: Mapped[UUID] = mapped_column(
        PG_UUID, ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_id: Mapped[UUID | None] = mapped_column(
        PG_UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    parent_id: Mapped[UUID | None] = mapped_column(
        PG_UUID, ForeignKey("comments.id", ondelete="CASCADE"), nullable=True, index=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    like_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    report_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_blinded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    author: Mapped[User | None] = relationship(User, foreign_keys=[author_id], lazy="raise_on_sql")


class CommentLike(Base):
    __tablename__ = "comment_likes"

    comment_id: Mapped[UUID] = mapped_column(
        PG_UUID, ForeignKey("comments.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
