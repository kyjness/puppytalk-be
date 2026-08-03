# 관리자 전용: 신고 게시글/댓글 목록·블라인드 해제·유저 정지·게시글 삭제. AsyncSession.

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.common import UserStatus
from app.common.enums import TargetType
from app.common.exceptions import (
    CommentNotFoundException,
    ConcurrentUpdateException,
    PostNotFoundException,
    UserNotFoundException,
    UserWithdrawnException,
)
from app.domain.admin.model import AdminReportsModel
from app.domain.admin.schema import ReportedPostAuthorInfo, ReportedPostItem
from app.domain.auth.service import AuthService
from app.domain.comments.repository import CommentsModel
from app.domain.posts.repository import PostsModel
from app.domain.posts.services import PostService
from app.domain.reports.model import ReportsModel
from app.domain.users.model import UsersModel

CONTENT_PREVIEW_LEN = 80  # 게시글/댓글 내용 미리보기 글자 수


def _content_preview(content: str | None) -> str:
    text = content or ""
    if len(text) > CONTENT_PREVIEW_LEN:
        return text[:CONTENT_PREVIEW_LEN] + "…"
    return text[:CONTENT_PREVIEW_LEN]


def _author_from_user(user) -> tuple[ReportedPostAuthorInfo | None, str | None]:
    if not user:
        return None, None
    status = getattr(user, "status", None)
    author = ReportedPostAuthorInfo(
        id=user.id,
        nickname=user.nickname,
        profile_image_url=getattr(user, "profile_image_url", None)
        or (user.profile_image.file_url if getattr(user, "profile_image", None) else None),
        status=status,
    )
    return author, status


class AdminService:
    @classmethod
    async def get_reported_posts(
        cls,
        page: int,
        size: int,
        db: AsyncSession,
    ) -> tuple[list[ReportedPostItem], int]:
        async with db.begin():
            # 신고된 게시글·댓글을 DB-side UNION ALL로 합쳐 정렬·페이지(#5). 인메모리 병합·cap 없이
            # 페이지 경계·total이 정확하다. 여기서 나온 (type, id) 순서를 그대로 유지해 하이드레이션한다.
            page_rows, total = await AdminReportsModel.page_reported_targets(
                offset=(page - 1) * size, size=size, db=db
            )
            post_ids = [tid for ttype, tid in page_rows if ttype == TargetType.POST.value]
            comment_ids = [tid for ttype, tid in page_rows if ttype == TargetType.COMMENT.value]

            posts = await PostsModel.get_reported_by_ids(post_ids, db=db)
            comments = await CommentsModel.get_reported_by_ids(comment_ids, db=db)
            last_at_by_post = await ReportsModel.bulk_max_created_at_by_target_ids(
                TargetType.POST, post_ids, db=db
            )
            last_at_by_comment = await ReportsModel.bulk_max_created_at_by_target_ids(
                TargetType.COMMENT, comment_ids, db=db
            )
            reasons_by_post = await ReportsModel.bulk_reasons_ordered_by_target_ids(
                TargetType.POST, post_ids, db=db
            )
            reasons_by_comment = await ReportsModel.bulk_reasons_ordered_by_target_ids(
                TargetType.COMMENT, comment_ids, db=db
            )
            titles_map = await PostsModel.get_titles_by_ids(
                list({c.post_id for c in comments}), db=db
            )

            posts_by_id = {p.id: p for p in posts}
            comments_by_id = {c.id: c for c in comments}

            items: list[ReportedPostItem] = []
            for ttype, tid in page_rows:
                if ttype == TargetType.POST.value:
                    p = posts_by_id.get(tid)
                    if p is None or p.user_id is None:
                        continue
                    author, author_status = _author_from_user(p.user)
                    items.append(
                        ReportedPostItem(
                            target_type="POST",
                            id=p.id,
                            post_id=p.id,
                            title=p.title or "",
                            content_preview=_content_preview(p.content),
                            user_id=p.user_id,
                            author=author,
                            author_status=author_status,
                            report_count=p.report_count,
                            report_reasons=reasons_by_post.get(p.id, []),
                            is_blinded=p.is_blinded,
                            created_at=p.created_at,
                            last_reported_at=last_at_by_post.get(p.id),
                        )
                    )
                else:
                    c = comments_by_id.get(tid)
                    if c is None or c.author_id is None:
                        continue
                    author, author_status = _author_from_user(c.author)
                    items.append(
                        ReportedPostItem(
                            target_type="COMMENT",
                            id=c.id,
                            post_id=c.post_id,
                            title=titles_map.get(c.post_id, ""),
                            content_preview=_content_preview(c.content),
                            user_id=c.author_id,
                            author=author,
                            author_status=author_status,
                            report_count=c.report_count,
                            report_reasons=reasons_by_comment.get(c.id, []),
                            is_blinded=c.is_blinded,
                            created_at=c.created_at,
                            last_reported_at=last_at_by_comment.get(c.id),
                        )
                    )
            return items, total

    @classmethod
    async def unblind_post(cls, post_id: UUID, db: AsyncSession) -> None:
        async with db.begin():
            try:
                ok = await PostsModel.unblind_post(post_id, db=db)
            except StaleDataError as e:
                raise ConcurrentUpdateException() from e
        if not ok:
            raise PostNotFoundException()

    @classmethod
    async def reset_post_reports(cls, post_id: UUID, db: AsyncSession) -> None:
        async with db.begin():
            await ReportsModel.delete_by_post_id(post_id, db=db)
            await db.flush()  # delete 반영 후 reset_reports 실행해 재신고 시 목록 노출 보장
            try:
                ok = await PostsModel.reset_reports(post_id, db=db)
            except StaleDataError as e:
                raise ConcurrentUpdateException() from e
        if not ok:
            raise PostNotFoundException()

    @classmethod
    async def suspend_user(cls, user_id: UUID, db: AsyncSession, redis: Any | None = None) -> None:
        async with db.begin():
            user = await UsersModel.get_user_by_id_including_deleted(user_id, db=db)
            if not user:
                raise UserNotFoundException()
            if getattr(user, "deleted_at", None) is not None or UserStatus.is_withdrawn_value(
                getattr(user, "status", None)
            ):
                raise UserWithdrawnException()
            await UsersModel.update_user(user_id, db=db, status=UserStatus.SUSPENDED.value)
        await AuthService.revoke_refresh_for_user(user_id, redis)
        await AuthService.invalidate_user_status_cache(redis, user_id)

    @classmethod
    async def activate_user(cls, user_id: UUID, db: AsyncSession, redis: Any | None = None) -> None:
        async with db.begin():
            user = await UsersModel.get_user_by_id_including_deleted(user_id, db=db)
            if not user:
                raise UserNotFoundException()
            if getattr(user, "deleted_at", None) is not None or UserStatus.is_withdrawn_value(
                getattr(user, "status", None)
            ):
                raise UserWithdrawnException()
            await UsersModel.update_user(user_id, db=db, status=UserStatus.ACTIVE.value)
        await AuthService.invalidate_user_status_cache(redis, user_id)

    @classmethod
    async def blind_post(cls, post_id: UUID, db: AsyncSession) -> None:
        async with db.begin():
            try:
                ok = await PostsModel.set_blinded(post_id, db=db)
            except StaleDataError as e:
                raise ConcurrentUpdateException() from e
        if not ok:
            raise PostNotFoundException()

    @classmethod
    async def delete_post(cls, post_id: UUID, db: AsyncSession) -> None:
        # 삭제 캐스케이드(댓글·좋아요·이미지)는 PostService가 단일 트랜잭션에서 조율한다.
        await PostService.delete_post(post_id, db=db)

    @classmethod
    async def unblind_comment(cls, comment_id: UUID, db: AsyncSession) -> None:
        async with db.begin():
            ok = await CommentsModel.unblind_comment(comment_id, db=db)
        if not ok:
            raise CommentNotFoundException()

    @classmethod
    async def blind_comment(cls, comment_id: UUID, db: AsyncSession) -> None:
        async with db.begin():
            ok = await CommentsModel.set_blinded(comment_id, db=db)
        if not ok:
            raise CommentNotFoundException()

    @classmethod
    async def reset_comment_reports(cls, comment_id: UUID, db: AsyncSession) -> None:
        async with db.begin():
            await ReportsModel.delete_by_comment_id(comment_id, db=db)
            await db.flush()
            ok = await CommentsModel.reset_reports(comment_id, db=db)
        if not ok:
            raise CommentNotFoundException()

    @classmethod
    async def delete_comment(cls, post_id: UUID, comment_id: UUID, db: AsyncSession) -> None:
        async with db.begin():
            # 삭제는 멱등이 아니므로, 먼저 대상 존재 확인 후 삭제/카운트 감소를 같은 트랜잭션에서 처리.
            meta = await CommentsModel.get_comment_meta(comment_id, db=db)
            if meta is None or meta.deleted_at is not None:
                raise CommentNotFoundException()
            deleted = await CommentsModel.delete_comment(post_id, comment_id, db=db)
            if not deleted:
                raise CommentNotFoundException()
            try:
                await PostsModel.decrement_comment_count(post_id, db=db)
            except StaleDataError as e:
                raise ConcurrentUpdateException() from e
