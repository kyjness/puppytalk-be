# 댓글 비즈니스 로직. Full-Async. 생성/삭제/블라인드 시 게시글 comment_count 조정은
# 서비스에서 조율한다 — 모더레이션 조율은 CommentModeration이 맡는다.

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
from app.domain.comments.repository import CommentLikesRepository, CommentsRepository
from app.domain.comments.schema import (
    CommentCreateRequest,
    CommentIdData,
    CommentResponse,
    CommentUpdateRequest,
)
from app.domain.notifications.schema import NotificationEvent
from app.domain.notifications.service import NotificationService
from app.domain.posts.repository import PostsRepository
from app.infra.redis import RedisLike

_DELETED_CONTENT_PLACEHOLDER = "삭제된 댓글입니다."

# 목록 응답에 루트당 함께 실어 보내는 대댓글 수. 서버 상수로 고정한다 — 클라이언트가
# 제어하면 한 요청이 끌어오는 행 수의 상한이 다시 사라진다(트렌딩 window_hours와 같은 이유).
# 나머지는 GET .../comments/{comment_id}/replies 로 이어 받는다.
REPLY_PREVIEW_LIMIT = 3


async def _ensure_post_visible(
    post_id: UUID,
    db: AsyncSession,
    current_user_id: UUID | None = None,
) -> None:
    if not await PostsRepository.post_is_visible(post_id, db=db, current_user_id=current_user_id):
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
    reply_previews: list[tuple],
    liked_ids: set,
    sort: str = "latest",
) -> list[CommentResponse]:
    """루트 순서는 keyset로 이미 확정돼 있으므로 보존하고, 대댓글 preview만 부모에 붙여 정렬한다.

    reply_previews는 (대댓글, 그 부모의 총 표시 가능 대댓글 수) 쌍이다 — 총 개수를 별도
    COUNT 쿼리 없이 같은 조회에서 받아, 응답의 reply_count·has_more_replies를 채운다.
    """
    root_resps = [_comment_to_response(r, liked_ids) for r in roots]
    by_id = {r.id: resp for r, resp in zip(roots, root_resps)}
    for rp, total in reply_previews:
        parent = by_id.get(rp.parent_id)
        if parent is not None:
            parent.replies.append(_comment_to_response(rp, liked_ids))
            parent.reply_count = total
    reverse = sort != "oldest"
    for resp in root_resps:
        resp.replies.sort(key=lambda x: x.id, reverse=reverse)
        resp.has_more_replies = resp.reply_count > len(resp.replies)
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
            visible = await PostsRepository.get_visible_post_author(
                post_id, db=db, current_user_id=user_id
            )
            if visible is None:
                raise PostNotFoundException()
            post_author_id = visible.author_id
            if data.parent_id is not None:
                parent = await CommentsRepository.get_comment_meta(data.parent_id, db=db)
                if (
                    parent is None
                    or parent.deleted_at is not None
                    or parent.post_id != post_id
                    or parent.parent_id is not None
                ):
                    raise CommentNotFoundException()
            comment = await CommentsRepository.create_comment(
                post_id, user_id, data.content, db=db, parent_id=data.parent_id
            )
            try:
                await PostsRepository.increment_comment_count(post_id, db=db)
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
            fetched = await CommentsRepository.get_root_comments(
                post_id,
                size,
                db=db,
                cursor=cursor,
                sort=sort_mode,
                current_user_id=current_user_id,
            )
            roots, has_more = split_page(fetched, size)
            reply_previews = await CommentsRepository.get_reply_previews_for_roots(
                [r.id for r in roots],
                db=db,
                limit_per_root=REPLY_PREVIEW_LIMIT,
                sort=sort_mode,
                current_user_id=current_user_id,
            )
            comment_ids = [c.id for c in roots] + [c.id for c, _ in reply_previews]
            liked_ids = (
                await CommentLikesRepository.get_liked_comment_ids_for_user(
                    current_user_id, comment_ids, db=db
                )
                if current_user_id is not None
                else set()
            )
            result = _build_comment_tree(roots, reply_previews, liked_ids, sort=sort_mode)
        return result, has_more

    @classmethod
    async def get_replies(
        cls,
        post_id: UUID,
        comment_id: UUID,
        size: int,
        db: AsyncSession,
        sort: str | None = None,
        cursor: UUID | None = None,
        current_user_id: UUID | None = None,
    ) -> tuple[list[CommentResponse], bool]:
        """한 루트의 대댓글 keyset 페이지 — 목록 응답의 preview 뒤를 이어 받는다."""
        sort_mode = sort if sort in ("latest", "oldest") else "latest"
        async with db.begin():
            await _ensure_post_visible(post_id, db=db, current_user_id=current_user_id)
            # 루트가 이 게시글에 속한 실제 루트인지 확인 — 대댓글 id로 조회해 트리를
            # 한 단계 더 파고드는 요청(2단 구조 위반)과 남의 글 id 조합을 함께 막는다.
            # 블라인드 루트도 막는다: 목록에서는 서브트리가 통째로 사라지는데 여기만
            # 열려 있으면 루트 공개 id를 아는 사람에게 모더레이션이 무력해진다.
            # (삭제 루트는 placeholder로 남으므로 대댓글 조회를 허용한다.)
            meta = await CommentsRepository.get_visible_root_meta(comment_id, db=db)
            if meta is None or meta.post_id != post_id or meta.parent_id is not None:
                raise CommentNotFoundException()
            fetched = await CommentsRepository.get_replies_page(
                comment_id,
                size,
                db=db,
                cursor=cursor,
                sort=sort_mode,
                current_user_id=current_user_id,
            )
            replies, has_more = split_page(fetched, size)
            liked_ids = (
                await CommentLikesRepository.get_liked_comment_ids_for_user(
                    current_user_id, [c.id for c in replies], db=db
                )
                if current_user_id is not None
                else set()
            )
            items = [_comment_to_response(r, liked_ids) for r in replies]
        return items, has_more

    @classmethod
    async def update_comment(
        cls,
        post_id: UUID,
        comment_id: UUID,
        data: CommentUpdateRequest,
        db: AsyncSession,
    ) -> None:
        async with db.begin():
            if not await CommentsRepository.update_comment(
                post_id, comment_id, data.content, db=db
            ):
                raise CommentNotFoundException()

    @classmethod
    async def delete_comment(cls, post_id: UUID, comment_id: UUID, db: AsyncSession) -> None:
        async with db.begin():
            # UPDATE 우선(행복 경로 1쿼리) — 표시 중이던 댓글을 지운 경우에만 post_id가 온다.
            deleted_from = await CommentsRepository.delete_comment(post_id, comment_id, db=db)
            if deleted_from is not None:
                try:
                    await PostsRepository.decrement_comment_count(deleted_from, db=db)
                except StaleDataError as e:
                    raise ConcurrentUpdateException() from e
                return
            # 여기까지 오는 셋: 미존재 · 이미 삭제 · 방금 지운 블라인드 댓글.
            # 뒤의 둘은 deleted_at이 채워져 있어 멱등 204로 수렴한다.
            meta = await CommentsRepository.get_comment_meta(comment_id, db=db)
            if meta is None or meta.post_id != post_id or meta.deleted_at is None:
                raise CommentNotFoundException()
            return


class CommentModeration:
    """댓글 모더레이션 파사드 — 블라인드 전이에 맞춰 게시글 comment_count까지 조율한다.

    `comment_count`의 정의는 **표시 가능한(미삭제·미블라인드) 댓글 수**다. 목록 조회가
    블라인드 댓글을 빼고 내려주므로(`_reply_visible_conditions`), 블라인드가 카운트를
    건드리지 않으면 "댓글 5개"라고 표시하면서 4개만 보이는 불일치가 생긴다.

    저장소가 아니라 여기서 조율하는 이유는 생성·삭제 경로와 같다 — 게시글 카운트는
    댓글 도메인의 서비스가 소유한다. `reports/targets.py`의 모더레이션 배선이 COMMENT
    타깃으로 이 클래스를 가리켜, AdminService·ReportService는 분기 없이 그대로 쓴다.
    """

    @classmethod
    async def increment_report_count(
        cls, comment_id: UUID, /, db: AsyncSession
    ) -> int | None:  # 카운트 조율과 무관 — 저장소로 위임(모더레이션 계약 충족용).
        return await CommentsRepository.increment_report_count(comment_id, db=db)

    @classmethod
    async def _resync_count(cls, post_id: UUID, *, db: AsyncSession) -> None:
        """블라인드 전이 뒤 게시글 카운트를 다시 세어 넣는다.

        ±1로는 맞출 수 없다 — 루트를 블라인드하면 대댓글까지 목록에서 사라지므로 실제
        변화량은 1 + (그 루트의 표시 가능 대댓글 수)다. 모더레이션은 드문 경로라
        COUNT 한 번이 싸고, 서브트리 크기를 손으로 세다 어긋나는 종류를 통째로 없앤다.
        """
        await PostsRepository.set_comment_count(
            post_id, await CommentsRepository.count_visible_for_post(post_id, db=db), db=db
        )

    @classmethod
    async def set_blinded(cls, comment_id: UUID, /, db: AsyncSession) -> bool:
        post_id = await CommentsRepository.blind_if_visible(comment_id, db=db)
        if post_id is not None:
            await cls._resync_count(post_id, db=db)
            return True
        # 전이가 없었다 — 이미 블라인드(멱등 성공)인지 미존재·삭제(404)인지 가른다.
        return await cls._is_alive(comment_id, db=db)

    @classmethod
    async def unblind(cls, comment_id: UUID, /, db: AsyncSession) -> bool:
        post_id = await CommentsRepository.unblind_if_blinded(comment_id, db=db)
        if post_id is not None:
            await cls._resync_count(post_id, db=db)
            return True
        return await cls._is_alive(comment_id, db=db)

    @classmethod
    async def reset_reports(cls, comment_id: UUID, /, db: AsyncSession) -> bool:
        # reset은 블라인드 해제를 겸한다 — 해제 전이가 있었다면 카운트도 되돌린다.
        await cls.unblind(comment_id, db=db)
        return await CommentsRepository.reset_reports(comment_id, db=db)

    @classmethod
    async def _is_alive(cls, comment_id: UUID, *, db: AsyncSession) -> bool:
        meta = await CommentsRepository.get_comment_meta(comment_id, db=db)
        return meta is not None and meta.deleted_at is None
