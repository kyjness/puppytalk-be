# Likes 도메인 서비스. Full-Async. 중복 좋아요는 ON CONFLICT DO NOTHING(inserted=False)으로 처리.
# 하나의 요청당 하나의 async with db.begin()으로 묶어 Race Condition 방지.
# post·comment 좋아요는 같은 흐름의 타깃 치환 — _Target 하나로 파라미터화한다.
# like는 뷰어 가시성(차단·블라인드 404)을 검사하지만, unlike는 "회수"라 뷰어 의존 술어를
# 타지 않는다 — 차단·블라인드 이후에도 자기 좋아요는 되돌릴 수 있어야 한다(고착 방지).

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
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
from app.domain.notifications.schema import NotificationEvent
from app.domain.notifications.service import NotificationService
from app.domain.posts.repository import PostsModel
from app.infra.redis import RedisLike


class _LikeLinks(Protocol):
    """좋아요 링크 행 repo 계약. 첫 인자는 positional-only — 구현의 post_id/comment_id
    이름 차이를 흡수한다(PostLikesModel·CommentLikesModel이 구조적으로 적합)."""

    async def create(self, target_id: UUID, user_id: UUID, /, *, db: AsyncSession) -> bool: ...
    async def delete(self, target_id: UUID, user_id: UUID, /, *, db: AsyncSession) -> bool: ...


class _LikeCounters(Protocol):
    """like_count 카운터 소유 repo 계약(PostsModel·CommentsModel이 구조적으로 적합)."""

    async def increment_like_count(self, target_id: UUID, /, *, db: AsyncSession) -> int: ...
    async def decrement_like_count(self, target_id: UUID, /, *, db: AsyncSession) -> int: ...
    async def get_like_count(self, target_id: UUID, /, *, db: AsyncSession) -> int: ...


@dataclass(frozen=True, slots=True)
class _LikeNotify:
    """like 가시성 검증을 통과한 타깃의 알림 조립 정보."""

    author_id: UUID | None
    kind: NotificationKind
    post_id: UUID | None
    comment_id: UUID | None


@dataclass(frozen=True, slots=True)
class _Target:
    """타깃별 정적 배선 — 링크·카운터 repo와 like/unlike 각각의 타깃 확인."""

    links: _LikeLinks
    counters: _LikeCounters
    resolve_for_like: Callable[[UUID, UUID, AsyncSession], Awaitable[_LikeNotify]]
    ensure_unlikeable: Callable[[UUID, AsyncSession], Awaitable[None]]


async def _resolve_post_for_like(post_id: UUID, user_id: UUID, db: AsyncSession) -> _LikeNotify:
    """가시성 확인 + 작성자(알림 수신자) 조회를 1쿼리로 — 차단 술어 포함(목록·댓글과 동일)."""
    visible = await PostsModel.get_visible_post_author(post_id, db=db, current_user_id=user_id)
    if visible is None:
        raise PostNotFoundException()
    return _LikeNotify(
        author_id=visible.author_id,
        kind=NotificationKind.LIKE_POST,
        post_id=post_id,
        comment_id=None,
    )


async def _ensure_post_unlikeable(post_id: UUID, db: AsyncSession) -> None:
    """회수 가드: 미삭제·미블라인드만 — 차단 술어 없음(차단해도 자기 좋아요는 회수 가능)."""
    if not await PostsModel.post_is_visible(post_id, db=db):
        raise PostNotFoundException()


async def _resolve_comment_for_like(
    comment_id: UUID, user_id: UUID, db: AsyncSession
) -> _LikeNotify:
    meta = await CommentsModel.get_visible_comment_meta(comment_id, db=db, current_user_id=user_id)
    if meta is None:
        raise CommentNotFoundException()
    return _LikeNotify(
        author_id=meta.author_id,
        kind=NotificationKind.LIKE_COMMENT,
        post_id=meta.post_id,
        comment_id=comment_id,
    )


async def _ensure_comment_unlikeable(comment_id: UUID, db: AsyncSession) -> None:
    """회수 가드: 삭제만 검사 — 블라인드·차단은 회수를 막지 않는다."""
    meta = await CommentsModel.get_comment_meta(comment_id, db=db)
    if meta is None or meta.deleted_at is not None:
        raise CommentNotFoundException()


_POST = _Target(
    links=PostLikesModel,
    counters=PostsModel,
    resolve_for_like=_resolve_post_for_like,
    ensure_unlikeable=_ensure_post_unlikeable,
)
_COMMENT = _Target(
    links=CommentLikesModel,
    counters=CommentsModel,
    resolve_for_like=_resolve_comment_for_like,
    ensure_unlikeable=_ensure_comment_unlikeable,
)


class LikeService:
    @classmethod
    async def like_post(
        cls, post_id: UUID, user_id: UUID, db: AsyncSession, redis: RedisLike | None = None
    ) -> tuple[int, bool]:
        return await cls._like(_POST, post_id, user_id, db, redis)

    @classmethod
    async def unlike_post(cls, post_id: UUID, user_id: UUID, db: AsyncSession) -> int:
        return await cls._unlike(_POST, post_id, user_id, db)

    @classmethod
    async def like_comment(
        cls, comment_id: UUID, user_id: UUID, db: AsyncSession, redis: RedisLike | None = None
    ) -> tuple[int, bool]:
        return await cls._like(_COMMENT, comment_id, user_id, db, redis)

    @classmethod
    async def unlike_comment(cls, comment_id: UUID, user_id: UUID, db: AsyncSession) -> int:
        return await cls._unlike(_COMMENT, comment_id, user_id, db)

    @classmethod
    async def _like(
        cls,
        target: _Target,
        target_id: UUID,
        user_id: UUID,
        db: AsyncSession,
        redis: RedisLike | None,
    ) -> tuple[int, bool]:
        """(like_count, inserted) 반환. 자기 글이 아니면 커밋 후 작성자에게 알림 발행."""
        event: NotificationEvent | None = None
        async with db.begin():
            notify = await target.resolve_for_like(target_id, user_id, db)
            try:
                inserted = await target.links.create(target_id, user_id, db=db)
                if inserted:
                    like_count = await target.counters.increment_like_count(target_id, db=db)
                    if notify.author_id and notify.author_id != user_id:
                        event = await NotificationService.record(
                            recipient_user_id=notify.author_id,
                            kind=notify.kind,
                            actor_id=user_id,
                            post_id=notify.post_id,
                            comment_id=notify.comment_id,
                            db=db,
                        )
                else:
                    like_count = await target.counters.get_like_count(target_id, db=db)
            except StaleDataError as e:
                raise ConcurrentUpdateException() from e
        if event is not None:
            await NotificationService.publish_after_commit(redis, event)
        return like_count, inserted

    @classmethod
    async def _unlike(
        cls, target: _Target, target_id: UUID, user_id: UUID, db: AsyncSession
    ) -> int:
        """삭제 후 like_count 반환. 없던 좋아요 삭제는 no-op(현재 카운트만 반환)."""
        async with db.begin():
            await target.ensure_unlikeable(target_id, db)
            try:
                deleted = await target.links.delete(target_id, user_id, db=db)
                if deleted:
                    like_count = await target.counters.decrement_like_count(target_id, db=db)
                else:
                    like_count = await target.counters.get_like_count(target_id, db=db)
            except StaleDataError as e:
                raise ConcurrentUpdateException() from e
        return like_count
