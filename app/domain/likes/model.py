# Likes 도메인 모델. 게시글 좋아요(PostLike) 테이블 및 CRUD. AsyncSession.
# comment_likes·CommentLikesRepository은 comments 도메인에 유지(순환 참조 방지).

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import PG_UUID, Base, utc_now
from app.db.statements import delete_rows, insert_ignore


class PostLike(Base):
    __tablename__ = "post_likes"

    post_id: Mapped[UUID] = mapped_column(
        PG_UUID, ForeignKey("posts.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        # PK가 (post_id, user_id)라 user_id는 선행 컬럼이 아니다 — 탈퇴 퍼지의 집계와
        # 유저 삭제 CASCADE가 이 인덱스 없이는 테이블 전체를 훑는다.
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PostLikesRepository:
    @classmethod
    async def get_liked_post_ids_for_user(
        cls, user_id: UUID, post_ids: list[UUID], db: AsyncSession
    ) -> set[UUID]:
        if not post_ids:
            return set()
        stmt = select(PostLike.post_id).where(
            PostLike.user_id == user_id,
            PostLike.post_id.in_(post_ids),
        )
        result = await db.execute(stmt)
        return set(result.scalars().all())

    @classmethod
    async def create(cls, post_id: UUID, user_id: UUID, *, db: AsyncSession) -> bool:
        return await insert_ignore(
            db,
            PostLike,
            {"post_id": post_id, "user_id": user_id, "created_at": utc_now()},
            [PostLike.post_id, PostLike.user_id],
        )

    @classmethod
    async def delete(cls, post_id: UUID, user_id: UUID, *, db: AsyncSession) -> bool:
        deleted = await delete_rows(
            db, PostLike, [PostLike.post_id == post_id, PostLike.user_id == user_id]
        )
        return deleted > 0

    @classmethod
    async def delete_by_post_id(cls, post_id: UUID, db: AsyncSession) -> int:
        return await delete_rows(db, PostLike, [PostLike.post_id == post_id])
