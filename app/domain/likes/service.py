# Likes 도메인 서비스. Full-Async. 중복 좋아요는 ON CONFLICT DO NOTHING(inserted=False)으로 처리.
# 하나의 요청당 하나의 async with db.begin()으로 묶어 Race Condition 방지.

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.common.enums import NotificationKind
from app.common.exceptions import (
    CommentNotFoundException,
    ConcurrentUpdateException,
    PostNotFoundException,
)
from app.domain.comments.repository import CommentLikesModel, CommentsModel
from app.domain.likes.model import PostLikesModel
from app.domain.notifications.model import NotificationsModel
from app.domain.notifications.service import NotificationService
from app.domain.posts.repository import PostsModel
from app.infra.redis import RedisLike


class LikeService:
    @classmethod
    async def is_post_liked(cls, post_id: UUID, user_id: UUID, db: AsyncSession) -> bool:
        return await PostLikesModel.has_like(post_id, user_id, db=db)

    @classmethod
    async def like_post(
        cls,
        post_id: UUID,
        user_id: UUID,
        db: AsyncSession,
        redis: RedisLike | None = None,
    ) -> tuple[bool, int, bool]:
        notify: (
            tuple[UUID, UUID, NotificationKind, UUID | None, UUID | None, UUID | None] | None
        ) = None
        inserted_out = False
        like_count_out = 0
        async with db.begin():
            if not await PostsModel.post_is_visible(post_id, db=db):
                raise PostNotFoundException()
            try:
                inserted = await PostLikesModel.create(post_id, user_id, db=db)
                if inserted:
                    like_count = await PostsModel.increment_like_count(post_id, db=db)
                    author_id = await PostsModel.get_post_author_id(post_id, db=db)
                    if author_id and author_id != user_id:
                        nid = await NotificationsModel.insert(
                            user_id=author_id,
                            kind=NotificationKind.LIKE_POST,
                            actor_id=user_id,
                            post_id=post_id,
                            comment_id=None,
                            db=db,
                        )
                        notify = (
                            author_id,
                            nid,
                            NotificationKind.LIKE_POST,
                            user_id,
                            post_id,
                            None,
                        )
                else:
                    like_count = await PostsModel.get_like_count(post_id, db=db)
                inserted_out = inserted
                like_count_out = like_count
            except StaleDataError as e:
                raise ConcurrentUpdateException() from e
        if notify is not None:
            rec, nid, kind, act, pid, cid = notify
            await NotificationService.publish_after_commit(
                redis,
                recipient_user_id=rec,
                notification_id=nid,
                kind=kind,
                actor_id=act,
                post_id=pid,
                comment_id=cid,
            )
        return (True, like_count_out, inserted_out)

    @classmethod
    async def unlike_post(cls, post_id: UUID, user_id: UUID, db: AsyncSession) -> tuple[bool, int]:
        async with db.begin():
            if not await PostsModel.post_is_visible(post_id, db=db):
                raise PostNotFoundException()
            try:
                deleted = await PostLikesModel.delete(post_id, user_id, db=db)
                if deleted:
                    like_count = await PostsModel.decrement_like_count(post_id, db=db)
                else:
                    like_count = await PostsModel.get_like_count(post_id, db=db)
            except StaleDataError as e:
                raise ConcurrentUpdateException() from e
        return (False, like_count)

    @classmethod
    async def like_comment(
        cls,
        comment_id: UUID,
        user_id: UUID,
        db: AsyncSession,
        redis: RedisLike | None = None,
    ) -> tuple[bool, int, bool]:
        notify: (
            tuple[UUID, UUID, NotificationKind, UUID | None, UUID | None, UUID | None] | None
        ) = None
        inserted_out = False
        like_count_out = 0
        async with db.begin():
            comment_row = await CommentsModel.get_comment_meta(comment_id, db=db)
            if comment_row is None or comment_row.deleted_at is not None:
                raise CommentNotFoundException()
            try:
                inserted = await CommentLikesModel.create(comment_id, user_id, db=db)
                if inserted:
                    like_count = await CommentsModel.increment_like_count(comment_id, db=db)
                    author_id = comment_row.author_id
                    if author_id and author_id != user_id:
                        nid = await NotificationsModel.insert(
                            user_id=author_id,
                            kind=NotificationKind.LIKE_COMMENT,
                            actor_id=user_id,
                            post_id=comment_row.post_id,
                            comment_id=comment_id,
                            db=db,
                        )
                        notify = (
                            author_id,
                            nid,
                            NotificationKind.LIKE_COMMENT,
                            user_id,
                            comment_row.post_id,
                            comment_id,
                        )
                else:
                    like_count = await CommentsModel.get_like_count(comment_id, db=db)
                inserted_out = inserted
                like_count_out = like_count
            except StaleDataError as e:
                raise ConcurrentUpdateException() from e
        if notify is not None:
            rec, nid, kind, act, pid, cid = notify
            await NotificationService.publish_after_commit(
                redis,
                recipient_user_id=rec,
                notification_id=nid,
                kind=kind,
                actor_id=act,
                post_id=pid,
                comment_id=cid,
            )
        return (True, like_count_out, inserted_out)

    @classmethod
    async def unlike_comment(
        cls, comment_id: UUID, user_id: UUID, db: AsyncSession
    ) -> tuple[bool, int]:
        async with db.begin():
            meta = await CommentsModel.get_comment_meta(comment_id, db=db)
            if meta is None or meta.deleted_at is not None:
                raise CommentNotFoundException()
            try:
                deleted = await CommentLikesModel.delete(comment_id, user_id, db=db)
                if deleted:
                    like_count = await CommentsModel.decrement_like_count(comment_id, db=db)
                else:
                    like_count = await CommentsModel.get_like_count(comment_id, db=db)
            except StaleDataError as e:
                raise ConcurrentUpdateException() from e
        return (False, like_count)
