# 댓글 데이터 접근. ORM은 .model 참조(posts와 동형 분리).

from typing import Any, NamedTuple
from uuid import UUID

from sqlalchemy import and_, case, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, joinedload

from app.db.base_class import utc_now
from app.db.statements import (
    decrement_counter_by_link_owner,
    delete_rows,
    insert_ignore,
    update_one_returning,
)
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
    get_reply_previews_for_roots·get_replies_page가 이 술어를 공유해,
    삭제 루트 placeholder 시맨틱과 목록·더보기의 가시성 규칙이 어긋나지 않게 한다.
    """
    conds = [reply.deleted_at.is_(None), reply.is_blinded.is_(False)]
    not_blocked = author_not_blocked_clause(reply.author_id, current_user_id)
    if not_blocked is not None:
        conds.append(not_blocked)
    return conds


def _apply_id_keyset(stmt, *, sort: str, cursor: UUID | None, size: int):
    """id 기준 keyset 페이지네이션 공통 — 루트 목록과 대댓글 "더보기"가 공유한다.

    비교 방향과 정렬 방향이 어긋나면 페이지가 조용히 겹치거나 빠진다. 규칙을 한 곳에 둔다.
    size+1로 오버페치해 호출부가 split_page로 has_more를 가른다.
    """
    if sort == "oldest":
        if cursor is not None:
            stmt = stmt.where(Comment.id > cursor)
        stmt = stmt.order_by(Comment.id.asc())
    else:
        if cursor is not None:
            stmt = stmt.where(Comment.id < cursor)
        stmt = stmt.order_by(Comment.id.desc())
    return stmt.limit(size + 1)


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


class CommentsRepository:
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
    async def get_visible_root_meta(
        cls, comment_id: UUID, *, db: AsyncSession
    ) -> CommentMeta | None:
        """블라인드되지 않은 댓글의 메타 — 대댓글 "더보기"의 루트 가드용.

        삭제(deleted_at)는 막지 않는다: 삭제 루트는 목록에 placeholder로 남고 그 대댓글도
        계속 보이므로 여기서도 열려 있어야 한다. 블라인드는 반대로 서브트리를 감춘다.
        """
        return await cls._select_comment_meta(comment_id, db, Comment.is_blinded.is_(False))

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
        (EXISTS를 SQL에서 걸어 페이지 크기를 정확히 유지). 대댓글 preview는
        get_reply_previews_for_roots가 루트별로 상한을 두고 로드한다.
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
        stmt = _apply_id_keyset(stmt, sort=sort, cursor=cursor, size=size)
        result = await db.execute(stmt)
        return list(result.unique().scalars().all())

    @classmethod
    async def get_reply_previews_for_roots(
        cls,
        root_ids: list[UUID],
        *,
        db: AsyncSession,
        limit_per_root: int,
        sort: str = "latest",
        current_user_id: UUID | None = None,
    ) -> list[tuple[Comment, int]]:
        """루트별 대댓글 preview(최대 limit_per_root건)와 **부모별 총 표시 가능 개수**를 함께 준다.

        이전 구현은 `parent_id IN (root_ids)`로 대댓글을 전부 로드해 상한이 없었다 —
        인기 스레드의 루트 하나에 대댓글이 수천 건이면 한 응답이 그만큼의 행 + 작성자
        eager load를 끌어왔다(운영 봉투의 핫스팟 전제와 정면 배치).

        윈도우 함수로 루트당 상위 N건만 남기고 총 개수를 같은 쿼리에서 얻는다 —
        쿼리 1회, 행 수는 `루트 수 × N`으로 상한이 잡힌다. 가시성 술어는 목록·더보기와
        같은 _reply_visible_conditions를 공유해 규칙이 갈라지지 않는다.
        정렬 방향은 루트 정렬과 같게 둔다(기존 트리 조립 시맨틱 보존).
        """
        if not root_ids:
            return []
        order_col = Comment.id.asc() if sort == "oldest" else Comment.id.desc()
        visible = _reply_visible_conditions(Comment, current_user_id)
        ranked = (
            select(
                Comment.id.label("cid"),
                func.row_number()
                .over(partition_by=Comment.parent_id, order_by=order_col)
                .label("rn"),
                # 같은 파티션 스캔에서 총 개수까지 얻는다 — COUNT 쿼리를 따로 돌지 않는다.
                func.count().over(partition_by=Comment.parent_id).label("reply_total"),
            )
            .where(Comment.parent_id.in_(root_ids), *visible)
            .subquery()
        )
        stmt = (
            select(Comment, ranked.c.reply_total)
            .join(ranked, Comment.id == ranked.c.cid)
            .where(ranked.c.rn <= limit_per_root)
            .options(*_comment_author_loads())
        )
        rows = (await db.execute(stmt)).unique().all()
        return [(row[0], int(row[1] or 0)) for row in rows]

    @classmethod
    async def get_replies_page(
        cls,
        parent_id: UUID,
        size: int,
        *,
        db: AsyncSession,
        cursor: UUID | None = None,
        sort: str = "latest",
        current_user_id: UUID | None = None,
    ) -> list[Comment]:
        """한 루트의 대댓글을 keyset로 조회한다(size+1건으로 has_more 판정) — "더보기" 전용.

        루트 목록(get_root_comments)과 같은 keyset 패턴·같은 가시성 술어를 쓴다.
        """
        stmt = (
            select(Comment)
            .where(
                Comment.parent_id == parent_id,
                *_reply_visible_conditions(Comment, current_user_id),
            )
            .options(*_comment_author_loads())
        )
        result = await db.execute(_apply_id_keyset(stmt, sort=sort, cursor=cursor, size=size))
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
    async def delete_comment(cls, post_id: UUID, comment_id: UUID, db: AsyncSession) -> UUID | None:
        """soft-delete. **표시 중이던** 댓글을 지웠을 때만 post_id 반환 — 형제 전이 메서드
        (blind_if_visible·unblind_if_blinded)와 같은 규약이다.

        None은 셋을 뭉뚱그린다: 미존재·이미 삭제·블라인드된 댓글. 셋 다 카운트를 건드리면
        안 된다는 점에서 같고(블라인드분은 블라인드 시점에 이미 차감됐다), 404인지 멱등 204인지는
        호출부가 get_comment_meta로 가른다. 블라인드된 댓글은 삭제는 되지만 값이 NULL이라
        그 경로로 흘러 204가 된다 — 의도한 결과다.

        is_blinded를 WHERE에 넣으면 블라인드 댓글이 아예 삭제되지 않으므로 RETURNING에서 가른다.
        이 UPDATE는 is_blinded를 건드리지 않아 반환값이 삭제 직전 상태를 반영한다.
        """
        return await update_one_returning(
            db,
            Comment,
            [Comment.id == comment_id, Comment.post_id == post_id, Comment.deleted_at.is_(None)],
            {"deleted_at": utc_now()},
            case((Comment.is_blinded.is_(False), Comment.post_id), else_=None),
        )

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

    # --- 블라인드 전이 ---
    # 무조건 UPDATE(is_blinded=True)를 쓰지 않는 이유: 게시글 comment_count를 전이당 정확히
    # 한 번만 조정해야 하는데, 상태를 읽고-나서-쓰면 동시 모더레이션이 같은 전이를 둘 다
    # 관측해 이중 조정이 난다. 조건부 UPDATE로 전이 자체를 원자적으로 판정하고, 전이가
    # 실제로 일어났을 때만 소속 post_id를 돌려준다 — 조정은 CommentModeration이 수행한다.
    # alive 가드 — 삭제 댓글 blind는 404 판정(posts와 동일 시맨틱).

    @classmethod
    async def blind_if_visible(cls, comment_id: UUID, db: AsyncSession) -> UUID | None:
        """미블라인드 → 블라인드 전이면 post_id, 이미 블라인드거나 삭제·미존재면 None."""
        return await update_one_returning(
            db,
            Comment,
            [
                Comment.id == comment_id,
                Comment.deleted_at.is_(None),
                Comment.is_blinded.is_(False),
            ],
            {"is_blinded": True, "updated_at": utc_now()},
            Comment.post_id,
        )

    @classmethod
    async def unblind_if_blinded(cls, comment_id: UUID, db: AsyncSession) -> UUID | None:
        """블라인드 → 미블라인드 전이면 post_id, 이미 미블라인드거나 삭제·미존재면 None."""
        return await update_one_returning(
            db,
            Comment,
            [
                Comment.id == comment_id,
                Comment.deleted_at.is_(None),
                Comment.is_blinded.is_(True),
            ],
            {"is_blinded": False, "updated_at": utc_now()},
            Comment.post_id,
        )

    @classmethod
    async def reset_reports(cls, comment_id: UUID, db: AsyncSession) -> bool:
        """신고 카운트만 0으로. 블라인드 해제는 CommentModeration이 전이로 처리한다 —
        여기서 is_blinded까지 건드리면 같은 해제가 두 번 일어나고 카운트 조정과 어긋난다."""
        return (
            await _update_comment(db, comment_id, alive=True, touch=True, report_count=0)
            is not None
        )

    @classmethod
    async def count_visible_for_post(cls, post_id: UUID, *, db: AsyncSession) -> int:
        """게시글의 표시 가능한 댓글 수 — 루트가 블라인드되면 그 **서브트리 전체**가 목록에서
        사라지므로, 목록이 실제로 보여주는 것과 같은 규칙으로 센다.

        루트: 미삭제·미블라인드. 대댓글: 부모가 살아 있고 자신도 미삭제·미블라인드.
        (차단은 사용자별이라 카운트에 반영하지 않는다 — 표시 개수는 사용자 무관 값이다.)
        """
        parent = aliased(Comment)
        alive_root = and_(Comment.deleted_at.is_(None), Comment.is_blinded.is_(False))
        stmt = (
            select(func.count())
            .select_from(Comment)
            .outerjoin(parent, Comment.parent_id == parent.id)
            .where(
                Comment.post_id == post_id,
                alive_root,
                or_(
                    Comment.parent_id.is_(None),
                    and_(parent.deleted_at.is_(None), parent.is_blinded.is_(False)),
                ),
            )
        )
        return int((await db.execute(stmt)).scalar_one() or 0)

    @classmethod
    async def decrement_like_counts_for_users(cls, user_ids: list[UUID], db: AsyncSession) -> None:
        """주어진 유저들이 누른 좋아요만큼 댓글 like_count를 깎는다 — 유저 하드 삭제 **직전** 호출.

        comment_likes.user_id는 CASCADE, comments.author_id는 SET NULL이라 유저를 지우면
        좋아요 행만 사라지고 댓글은 남는다(posts와 동일한 드리프트).
        """
        await decrement_counter_by_link_owner(
            db,
            target_model=Comment,
            counter_col=Comment.like_count,
            link_target_col=CommentLike.comment_id,
            link_owner_col=CommentLike.user_id,
            owner_ids=user_ids,
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


class CommentLikesRepository:
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
