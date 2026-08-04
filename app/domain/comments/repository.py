# 댓글 데이터 접근. ORM은 .model 참조(posts와 동형 분리).

from typing import Any, NamedTuple
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, joinedload

from app.db.base_class import utc_now
from app.db.statements import delete_rows, insert_ignore, update_one_returning
from app.domain.posts.model import Post
from app.domain.users.model import User, author_display_loads, author_not_blocked_clause

from .model import Comment, CommentLike


class CommentAuthorPermissionRow(NamedTuple):
    comment_id: UUID | None
    comment_post_id: UUID | None
    comment_author_id: UUID | None


class CommentMeta(NamedTuple):
    """존재·소속·작성자·삭제 여부 판정용 경량 메타 — 작성자 hydrate 없이 컬럼만 읽는다."""

    post_id: UUID
    parent_id: UUID | None
    author_id: UUID | None
    deleted_at: object | None


def _comment_author_loads():
    """댓글 작성자 공통 eager load(users 공용 로더 — posts와 동일 정책)."""
    return (author_display_loads(Comment.author),)


def _reply_visible_conditions(reply, current_user_id: UUID | None) -> list:
    """표시 가능한 대댓글 조건: 미삭제·미블라인드·(차단 작성자 제외).

    루트의 'EXISTS 대댓글이 있으면 삭제 루트를 placeholder로 유지' 판정과
    get_replies_for_roots가 이 술어를 공유해, 삭제 루트 placeholder 시맨틱이 어긋나지 않게 한다.
    """
    conds = [reply.deleted_at.is_(None), reply.is_blinded.is_(False)]
    not_blocked = author_not_blocked_clause(reply.author_id, current_user_id)
    if not_blocked is not None:
        conds.append(not_blocked)
    return conds


async def _update_comment(
    db: AsyncSession,
    comment_id: UUID,
    *,
    alive: bool = False,
    touch: bool = False,
    returning: Any = Comment.id,
    **values,
):
    """댓글 카운터·모더레이션 공통 UPDATE…RETURNING.

    alive=deleted_at 가드, touch=updated_at 갱신 — 카운터류는 touch를 켜지 않는다
    (is_edited가 updated_at > created_at으로 판정되므로 켜면 '수정됨'으로 오인).
    """
    conds: list = [Comment.id == comment_id]
    if alive:
        conds.append(Comment.deleted_at.is_(None))
    if touch:
        values["updated_at"] = utc_now()
    return await update_one_returning(db, Comment, conds, values, returning)


class CommentsModel:
    @classmethod
    async def load_comment_author_permission_row(
        cls,
        post_id: UUID,
        comment_id: UUID,
        *,
        db: AsyncSession,
        include_deleted_comment: bool = False,
    ) -> CommentAuthorPermissionRow | None:
        """경로의 게시글이 존재·미삭제이면 1행. Comment는 LEFT JOIN (단일 SELECT).

        - include_deleted_comment=False: 삭제된 댓글은 JOIN에서 제외(미존재와 동일).
        - include_deleted_comment=True: 삭제된 댓글도 매칭(삭제 API 멱등).
        """
        join_on = [Comment.id == comment_id]
        if not include_deleted_comment:
            join_on.append(Comment.deleted_at.is_(None))
        stmt = (
            select(Comment.id, Comment.post_id, Comment.author_id)
            .select_from(Post)
            .outerjoin(Comment, and_(*join_on))
            .where(Post.id == post_id, Post.deleted_at.is_(None))
        )
        result = await db.execute(stmt)
        row = result.one_or_none()
        if row is None:
            return None
        cid, c_post_id, c_author_id = row
        return CommentAuthorPermissionRow(
            comment_id=cid,
            comment_post_id=c_post_id,
            comment_author_id=c_author_id,
        )

    @classmethod
    async def create_comment(
        cls,
        post_id: UUID,
        user_id: UUID,
        content: str,
        db: AsyncSession,
        parent_id: UUID | None = None,
    ) -> Comment:
        now = utc_now()
        c = Comment(
            post_id=post_id,
            author_id=user_id,
            parent_id=parent_id,
            content=content,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        db.add(c)
        await db.flush()
        return c

    @classmethod
    async def _select_comment_meta(
        cls, comment_id: UUID, db: AsyncSession, *extra_conds: Any
    ) -> CommentMeta | None:
        row = (
            await db.execute(
                select(
                    Comment.post_id, Comment.parent_id, Comment.author_id, Comment.deleted_at
                ).where(Comment.id == comment_id, *extra_conds)
            )
        ).one_or_none()
        if row is None:
            return None
        return CommentMeta(post_id=row[0], parent_id=row[1], author_id=row[2], deleted_at=row[3])

    @classmethod
    async def get_comment_meta(cls, comment_id: UUID, *, db: AsyncSession) -> CommentMeta | None:
        """부모 검증·멱등 삭제 판별용 — 삭제된 댓글도 매칭한다(deleted_at으로 구분)."""
        return await cls._select_comment_meta(comment_id, db)

    @classmethod
    async def get_visible_comment_meta(
        cls, comment_id: UUID, *, db: AsyncSession, current_user_id: UUID | None = None
    ) -> CommentMeta | None:
        """표시 가능한(미삭제·미블라인드·차단 작성자 제외) 댓글의 메타 — 좋아요 등 쓰기 가드용.

        가시성 규칙은 목록과 같은 _reply_visible_conditions 한 곳 — 보이지 않는 댓글에
        카운트·알림이 붙는 드리프트를 막는다.
        """
        return await cls._select_comment_meta(
            comment_id, db, *_reply_visible_conditions(Comment, current_user_id)
        )

    @classmethod
    async def get_root_comments(
        cls,
        post_id: UUID,
        size: int,
        *,
        db: AsyncSession,
        cursor: UUID | None = None,
        sort: str = "latest",
        current_user_id: UUID | None = None,
    ) -> list[Comment]:
        """루트 댓글을 keyset로 조회한다(size+1건으로 has_more 판정).

        삭제된 루트는 표시 가능한 대댓글이 하나라도 있을 때만 placeholder로 살린다
        (EXISTS를 SQL에서 걸어 페이지 크기를 정확히 유지). 대댓글은 get_replies_for_roots가
        부모별로 배치 로드한다.
        """
        reply = aliased(Comment)
        reply_exists = exists(1).where(
            reply.parent_id == Comment.id, *_reply_visible_conditions(reply, current_user_id)
        )
        stmt = (
            select(Comment)
            .where(
                Comment.post_id == post_id,
                Comment.parent_id.is_(None),
                Comment.is_blinded.is_(False),
                or_(Comment.deleted_at.is_(None), reply_exists),
            )
            .options(*_comment_author_loads())
        )
        root_not_blocked = author_not_blocked_clause(Comment.author_id, current_user_id)
        if root_not_blocked is not None:
            stmt = stmt.where(root_not_blocked)
        if sort == "oldest":
            if cursor is not None:
                stmt = stmt.where(Comment.id > cursor)
            stmt = stmt.order_by(Comment.id.asc())
        else:
            if cursor is not None:
                stmt = stmt.where(Comment.id < cursor)
            stmt = stmt.order_by(Comment.id.desc())
        stmt = stmt.limit(size + 1)
        result = await db.execute(stmt)
        return list(result.unique().scalars().all())

    @classmethod
    async def get_replies_for_roots(
        cls,
        root_ids: list[UUID],
        *,
        db: AsyncSession,
        current_user_id: UUID | None = None,
    ) -> list[Comment]:
        """주어진 루트들의 대댓글을 한 번에 배치 로드한다(부모별 하드리밋 없음).

        정렬은 _build_comment_tree가 부모별로 다시 하므로 여기선 SQL ORDER BY를 두지 않는다.
        """
        if not root_ids:
            return []
        stmt = (
            select(Comment)
            .where(
                Comment.parent_id.in_(root_ids),
                *_reply_visible_conditions(Comment, current_user_id),
            )
            .options(*_comment_author_loads())
        )
        result = await db.execute(stmt)
        return list(result.unique().scalars().all())

    @classmethod
    async def update_comment(
        cls, post_id: UUID, comment_id: UUID, content: str, db: AsyncSession
    ) -> bool:
        row = await update_one_returning(
            db,
            Comment,
            [Comment.id == comment_id, Comment.post_id == post_id, Comment.deleted_at.is_(None)],
            {"content": content, "updated_at": utc_now()},
            Comment.id,
        )
        return row is not None

    @classmethod
    async def delete_comment(cls, post_id: UUID, comment_id: UUID, db: AsyncSession) -> bool:
        row = await update_one_returning(
            db,
            Comment,
            [Comment.id == comment_id, Comment.post_id == post_id, Comment.deleted_at.is_(None)],
            {"deleted_at": utc_now()},
            Comment.id,
        )
        return row is not None

    @classmethod
    async def soft_delete_by_post(cls, post_id: UUID, db: AsyncSession) -> None:
        """게시글 삭제 캐스케이드용 — 해당 게시글의 미삭제 댓글 전체 soft-delete."""
        await db.execute(
            update(Comment)
            .where(Comment.post_id == post_id, Comment.deleted_at.is_(None))
            .values(deleted_at=utc_now())
        )

    @classmethod
    async def get_like_count(cls, comment_id: UUID, db: AsyncSession) -> int:
        result = await db.execute(select(Comment.like_count).where(Comment.id == comment_id))
        return result.scalar_one_or_none() or 0

    @classmethod
    async def get_reported_by_ids(
        cls, comment_ids: list[UUID], db: AsyncSession
    ) -> list["Comment"]:
        """신고 목록 하이드레이션용 id 배치 조회. 응답은 본문·작성자만 쓰므로
        작성자+프로필 이미지만 eager-load 한다(정렬·페이지는 UNION 쿼리가 담당)."""
        if not comment_ids:
            return []
        result = await db.execute(
            select(Comment)
            .where(Comment.id.in_(comment_ids))
            .options(joinedload(Comment.author).joinedload(User.profile_image))
        )
        return list(result.unique().scalars().all())

    @classmethod
    async def increment_report_count(cls, comment_id: UUID, db: AsyncSession) -> int | None:
        # 저자 없는 댓글(SET NULL)은 관리자 목록에 노출되지 않으므로 신고 대상에서도 제외 —
        # None 반환이 미존재·삭제·저자 없음의 404 판정을 겸한다(posts와 동일 시맨틱).
        return await update_one_returning(
            db,
            Comment,
            [
                Comment.id == comment_id,
                Comment.deleted_at.is_(None),
                Comment.author_id.isnot(None),
            ],
            {"report_count": Comment.report_count + 1},
            Comment.report_count,
        )

    @classmethod
    async def set_blinded(cls, comment_id: UUID, db: AsyncSession) -> bool:
        # alive 가드 — 삭제 댓글 blind는 404 판정(posts와 동일 시맨틱).
        return (
            await _update_comment(db, comment_id, alive=True, touch=True, is_blinded=True)
            is not None
        )

    @classmethod
    async def unblind(cls, comment_id: UUID, db: AsyncSession) -> bool:
        return (
            await _update_comment(db, comment_id, alive=True, touch=True, is_blinded=False)
            is not None
        )

    @classmethod
    async def reset_reports(cls, comment_id: UUID, db: AsyncSession) -> bool:
        return (
            await _update_comment(
                db, comment_id, alive=True, touch=True, report_count=0, is_blinded=False
            )
            is not None
        )

    @classmethod
    async def increment_like_count(cls, comment_id: UUID, db: AsyncSession) -> int:
        row = await _update_comment(
            db,
            comment_id,
            returning=Comment.like_count,
            like_count=Comment.like_count + 1,
        )
        return row if row is not None else 0

    @classmethod
    async def decrement_like_count(cls, comment_id: UUID, db: AsyncSession) -> int:
        row = await _update_comment(
            db,
            comment_id,
            returning=Comment.like_count,
            like_count=func.greatest(Comment.like_count - 1, 0),
        )
        return row if row is not None else 0


class CommentLikesModel:
    @classmethod
    async def get_liked_comment_ids_for_user(
        cls, user_id: UUID, comment_ids: list[UUID], db: AsyncSession
    ) -> set[UUID]:
        if not comment_ids:
            return set()
        stmt = select(CommentLike.comment_id).where(
            CommentLike.user_id == user_id,
            CommentLike.comment_id.in_(comment_ids),
        )
        result = await db.execute(stmt)
        return set(result.scalars().all())

    @classmethod
    async def create(cls, comment_id: UUID, user_id: UUID, *, db: AsyncSession) -> bool:
        return await insert_ignore(
            db,
            CommentLike,
            {"comment_id": comment_id, "user_id": user_id, "created_at": utc_now()},
            [CommentLike.comment_id, CommentLike.user_id],
        )

    @classmethod
    async def delete(cls, comment_id: UUID, user_id: UUID, *, db: AsyncSession) -> bool:
        deleted = await delete_rows(
            db,
            CommentLike,
            [CommentLike.comment_id == comment_id, CommentLike.user_id == user_id],
        )
        return deleted > 0
