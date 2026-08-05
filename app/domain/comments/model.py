# 댓글 ORM 정의. 쿼리/트랜잭션 로직은 repository.py (posts와 동형 분리).
# 관계는 쿼리가 실제 로드하는 것만 선언한다 — 대댓글 트리는 parent_id 컬럼으로 조립하므로
# parent/replies 관계 없음, 좋아요는 CommentLike 컬럼 직접 쿼리라 관계 없음.

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Text
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

    __table_args__ = (
        # 대댓글 preview(window function)·"더보기" keyset이 부모별로 정렬한다. 술어는
        # _reply_visible_conditions와 일치시켜 인덱스만으로 가시성까지 거른다.
        Index(
            "idx_comments_parent_visible",
            "parent_id",
            "id",
            postgresql_where=(deleted_at.is_(None)) & (is_blinded.is_(False)),
        ),
        # 루트 목록의 두 정렬 축(ADR 0016). offset 페이지는 매 요청 정렬을 타므로 순서까지
        # 인덱스로 받는다 — post_id 단일 인덱스만으로는 파티션 전체를 읽고 정렬해야 한다.
        # ASC로 선언해도 되는 이유: post_id 등치 뒤 두 축이 모두 DESC라 btree 역방향
        # 스캔으로 그대로 커버된다(방향이 섞일 때만 DESC 선언이 필요하다).
        # 부분 술어는 쿼리의 parent_id IS NULL·is_blinded=false와 일치시킨다. 삭제 루트는
        # placeholder로 살아남을 수 있어 deleted_at은 술어에 넣지 않는다.
        # 대가: like_count가 인덱스에 들어가므로 좋아요 UPDATE가 HOT 업데이트 경로를 잃는다
        # (변경 튜플이 이 행의 **모든** 인덱스에 새 엔트리를 만든다). 인기순 읽기를 위해
        # 쓰기 쪽에서 치르는 값이다 — 좋아요가 읽기보다 많아지면 재검토 대상.
        Index(
            "idx_comments_post_popular",
            "post_id",
            "like_count",
            "id",
            postgresql_where=(parent_id.is_(None)) & (is_blinded.is_(False)),
        ),
        Index(
            "idx_comments_post_latest",
            "post_id",
            "id",
            postgresql_where=(parent_id.is_(None)) & (is_blinded.is_(False)),
        ),
    )


class CommentLike(Base):
    __tablename__ = "comment_likes"

    comment_id: Mapped[UUID] = mapped_column(
        PG_UUID, ForeignKey("comments.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        # PostLike와 같은 이유 — PK 선행 컬럼이 아니라 퍼지 집계·CASCADE가 풀스캔이 된다.
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
