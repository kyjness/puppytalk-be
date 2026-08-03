# 댓글 비즈니스 로직. Full-Async. 생성/삭제 시 게시글 comment_count 조정은 서비스에서 조율.

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.common import split_page
from app.common.enums import NotificationKind
from app.common.exceptions import (
    CommentNotFoundException,
    ConcurrentUpdateException,
    PostNotFoundException,
)
from app.domain.comments.repository import CommentLikesModel, CommentsModel
from app.domain.comments.schema import (
    CommentCreateRequest,
    CommentIdData,
    CommentResponse,
    CommentUpdateRequest,
)
from app.domain.notifications.schema import NotificationEvent
from app.domain.notifications.service import NotificationService
from app.domain.posts.repository import PostsModel
from app.infra.redis import RedisLike

_DELETED_CONTENT_PLACEHOLDER = "삭제된 댓글입니다."


async def _ensure_post_visible(
    post_id: UUID,
    db: AsyncSession,
    current_user_id: UUID | None = None,
) -> None:
    if not await PostsModel.post_is_visible(post_id, db=db, current_user_id=current_user_id):
        raise PostNotFoundException()


def _comment_to_response(c, liked_ids: set):
    """AsyncSession에서는 lazy load 금지이므로, c.replies에 접근하지 않고 필드만 넣어 응답 생성."""
    is_edited = c.updated_at > c.created_at if (c.updated_at and c.created_at) else False
    is_deleted = c.deleted_at is not None
    return CommentResponse(
        id=c.id,
        content=c.content if not is_deleted else _DELETED_CONTENT_PLACEHOLDER,
        author=c.author,
        created_at=c.created_at,
        updated_at=c.updated_at,
        post_id=c.post_id,
        parent_id=c.parent_id,
        like_count=c.like_count,
        is_liked=c.id in liked_ids,
        is_edited=is_edited,
        is_deleted=is_deleted,
        replies=[],
    )


def _build_comment_tree(
    roots: list,
    replies: list,
    liked_ids: set,
    sort: str = "latest",
) -> list[CommentResponse]:
    """루트 순서는 keyset로 이미 확정돼 있으므로 보존하고, 대댓글만 부모에 붙여 정렬한다."""
    root_resps = [_comment_to_response(r, liked_ids) for r in roots]
    by_id = {r.id: resp for r, resp in zip(roots, root_resps)}
    for rp in replies:
        parent = by_id.get(rp.parent_id)
        if parent is not None:
            parent.replies.append(_comment_to_response(rp, liked_ids))
    reverse = sort != "oldest"
    for resp in root_resps:
        resp.replies.sort(key=lambda x: x.id, reverse=reverse)
    return root_resps


class CommentService:
    @classmethod
    async def create_comment(
        cls,
        post_id: UUID,
        user_id: UUID,
        data: CommentCreateRequest,
        db: AsyncSession,
        redis: RedisLike | None = None,
    ) -> CommentIdData:
        event: NotificationEvent | None = None
        async with db.begin():
            # 가시성 확인 + 작성자 조회를 1쿼리로(알림 수신자 판정에 작성자가 필요).
            visible = await PostsModel.get_visible_post_author(
                post_id, db=db, current_user_id=user_id
            )
            if visible is None:
                raise PostNotFoundException()
            post_author_id = visible.author_id
            if data.parent_id is not None:
                parent = await CommentsModel.get_comment_meta(data.parent_id, db=db)
                if (
                    parent is None
                    or parent.deleted_at is not None
                    or parent.post_id != post_id
                    or parent.parent_id is not None
                ):
                    raise CommentNotFoundException()
            comment = await CommentsModel.create_comment(
                post_id, user_id, data.content, db=db, parent_id=data.parent_id
            )
            try:
                await PostsModel.increment_comment_count(post_id, db=db)
            except StaleDataError as e:
                raise ConcurrentUpdateException() from e
            comment_id = comment.id
            if post_author_id and post_author_id != user_id:
                event = await NotificationService.record(
                    recipient_user_id=post_author_id,
                    kind=NotificationKind.COMMENT_ON_POST,
                    actor_id=user_id,
                    post_id=post_id,
                    comment_id=comment_id,
                    db=db,
                )
        if event is not None:
            await NotificationService.publish_after_commit(redis, event)
        return CommentIdData(id=comment_id)

    @classmethod
    async def get_comments(
        cls,
        post_id: UUID,
        size: int,
        db: AsyncSession,
        sort: str | None = None,
        cursor: UUID | None = None,
        current_user_id: UUID | None = None,
    ) -> tuple[list[CommentResponse], bool]:
        sort_mode = sort if sort in ("latest", "oldest") else "latest"
        async with db.begin():
            await _ensure_post_visible(post_id, db=db, current_user_id=current_user_id)
            fetched = await CommentsModel.get_root_comments(
                post_id,
                size,
                db=db,
                cursor=cursor,
                sort=sort_mode,
                current_user_id=current_user_id,
            )
            roots, has_more = split_page(fetched, size)
            replies = await CommentsModel.get_replies_for_roots(
                [r.id for r in roots], db=db, current_user_id=current_user_id
            )
            comment_ids = [c.id for c in roots] + [c.id for c in replies]
            liked_ids = (
                await CommentLikesModel.get_liked_comment_ids_for_user(
                    current_user_id, comment_ids, db=db
                )
                if current_user_id is not None
                else set()
            )
            result = _build_comment_tree(roots, replies, liked_ids, sort=sort_mode)
        return result, has_more

    @classmethod
    async def update_comment(
        cls,
        post_id: UUID,
        comment_id: UUID,
        data: CommentUpdateRequest,
        db: AsyncSession,
    ) -> None:
        async with db.begin():
            if not await CommentsModel.update_comment(post_id, comment_id, data.content, db=db):
                raise CommentNotFoundException()

    @classmethod
    async def delete_comment(cls, post_id: UUID, comment_id: UUID, db: AsyncSession) -> None:
        async with db.begin():
            # UPDATE 우선(행복 경로 1쿼리) — 0건이면 메타로 미존재/이미 삭제(멱등 204)를 가른다.
            if await CommentsModel.delete_comment(post_id, comment_id, db=db):
                try:
                    await PostsModel.decrement_comment_count(post_id, db=db)
                except StaleDataError as e:
                    raise ConcurrentUpdateException() from e
                return
            meta = await CommentsModel.get_comment_meta(comment_id, db=db)
            if meta is None or meta.post_id != post_id or meta.deleted_at is None:
                raise CommentNotFoundException()
            return  # 이미 삭제됨 → 204 (멱등)
