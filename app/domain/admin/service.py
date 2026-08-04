# 관리자 전용: 신고 게시글/댓글 목록·블라인드/해제·신고 리셋·유저 정지·삭제. AsyncSession.
# POST/COMMENT 분기는 reports/targets.py의 모더레이션 배선으로 흡수한다.

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.common import UserStatus
from app.common.enums import TargetType
from app.common.exceptions import UserNotFoundException, UserWithdrawnException
from app.domain.admin.model import AdminReportsModel
from app.domain.admin.schema import ReportedPostAuthorInfo, ReportedPostItem
from app.domain.auth.service import AuthService
from app.domain.comments.repository import CommentsModel
from app.domain.comments.service import CommentService
from app.domain.posts.repository import PostsModel
from app.domain.posts.services import PostService
from app.domain.reports.model import ReportsModel
from app.domain.reports.targets import moderation_target
from app.domain.users.model import UsersModel
from app.infra.redis import RedisLike

CONTENT_PREVIEW_LEN = 80  # 게시글/댓글 내용 미리보기 글자 수


def _content_preview(content: str | None) -> str:
    text = content or ""
    return text[:CONTENT_PREVIEW_LEN] + ("…" if len(text) > CONTENT_PREVIEW_LEN else "")


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
            post_ids = [tid for ttype, tid in page_rows if ttype is TargetType.POST]
            comment_ids = [tid for ttype, tid in page_rows if ttype is TargetType.COMMENT]

            posts = await PostsModel.get_reported_by_ids(post_ids, db=db)
            comments = await CommentsModel.get_reported_by_ids(comment_ids, db=db)
            reasons_map, last_at_map = await ReportsModel.bulk_report_meta(
                post_ids=post_ids, comment_ids=comment_ids, db=db
            )
            titles_map = await PostsModel.get_titles_by_ids(
                list({c.post_id for c in comments}), db=db
            )

            posts_by_id = {p.id: p for p in posts}
            comments_by_id = {c.id: c for c in comments}

            items: list[ReportedPostItem] = []
            for ttype, tid in page_rows:
                if ttype is TargetType.POST:
                    p = posts_by_id.get(tid)
                    if p is None or p.user_id is None:
                        continue
                    row = p
                    post_id = p.id
                    title = p.title or ""
                    user_id = p.user_id
                    user = p.user
                else:
                    c = comments_by_id.get(tid)
                    if c is None or c.author_id is None:
                        continue
                    row = c
                    post_id = c.post_id
                    title = titles_map.get(c.post_id, "")
                    user_id = c.author_id
                    user = c.author
                author = ReportedPostAuthorInfo.model_validate(user) if user else None
                items.append(
                    ReportedPostItem(
                        target_type=ttype.value,
                        id=tid,
                        post_id=post_id,
                        title=title,
                        content_preview=_content_preview(row.content),
                        user_id=user_id,
                        author=author,
                        author_status=author.status if author else None,
                        report_count=row.report_count,
                        report_reasons=reasons_map.get((ttype, tid), []),
                        is_blinded=row.is_blinded,
                        created_at=row.created_at,
                        last_reported_at=last_at_map.get((ttype, tid)),
                    )
                )
            return items, total

    # --- 모더레이션(블라인드/해제/신고 리셋) — 타깃 분기는 배선이 흡수 ---

    @classmethod
    async def blind(cls, target_type: TargetType, target_id: UUID, db: AsyncSession) -> None:
        t = moderation_target(target_type)
        async with db.begin():
            ok = await t.repo.set_blinded(target_id, db=db)
        if not ok:
            raise t.not_found()

    @classmethod
    async def unblind(cls, target_type: TargetType, target_id: UUID, db: AsyncSession) -> None:
        t = moderation_target(target_type)
        async with db.begin():
            ok = await t.repo.unblind(target_id, db=db)
        if not ok:
            raise t.not_found()

    @classmethod
    async def reset_reports(
        cls, target_type: TargetType, target_id: UUID, db: AsyncSession
    ) -> None:
        t = moderation_target(target_type)
        async with db.begin():
            await ReportsModel.delete_by_target(t.target_type, target_id, db=db)
            await db.flush()  # delete 반영 후 reset 실행해 재신고 시 목록 노출 보장
            ok = await t.repo.reset_reports(target_id, db=db)
        if not ok:
            raise t.not_found()

    # --- 유저 정지/복귀 ---

    @classmethod
    async def _set_user_status(
        cls,
        user_id: UUID,
        status: UserStatus,
        *,
        db: AsyncSession,
        redis: RedisLike | None,
        revoke_refresh: bool,
    ) -> None:
        async with db.begin():
            user = await UsersModel.get_user_by_id_including_deleted(user_id, db=db)
            if not user:
                raise UserNotFoundException()
            if user.deleted_at is not None or UserStatus.is_withdrawn_value(user.status):
                raise UserWithdrawnException()
            await UsersModel.update_user(user_id, db=db, status=status.value)
        if revoke_refresh:
            await AuthService.revoke_refresh_for_user(user_id, redis)
        await AuthService.invalidate_user_status_cache(redis, user_id)

    @classmethod
    async def suspend_user(
        cls, user_id: UUID, db: AsyncSession, redis: RedisLike | None = None
    ) -> None:
        # 정지는 재로그인 차단을 위해 refresh 토큰까지 회수한다.
        await cls._set_user_status(
            user_id, UserStatus.SUSPENDED, db=db, redis=redis, revoke_refresh=True
        )

    @classmethod
    async def activate_user(
        cls, user_id: UUID, db: AsyncSession, redis: RedisLike | None = None
    ) -> None:
        await cls._set_user_status(
            user_id, UserStatus.ACTIVE, db=db, redis=redis, revoke_refresh=False
        )

    # --- 삭제 — 캐스케이드·멱등 시맨틱은 각 도메인 서비스가 소유 ---

    @classmethod
    async def delete_post(cls, post_id: UUID, db: AsyncSession) -> None:
        # 삭제 캐스케이드(댓글·좋아요·이미지)는 PostService가 단일 트랜잭션에서 조율한다.
        await PostService.delete_post(post_id, db=db)

    @classmethod
    async def delete_comment(cls, post_id: UUID, comment_id: UUID, db: AsyncSession) -> None:
        # 본가 흐름에 위임해 캐스케이드 소유를 comments에 남긴다. 본가는 delete-first
        # 멱등이라 이미 삭제된 댓글도 200 — post 이중 삭제(404)와는 시맨틱이 다르다.
        await CommentService.delete_comment(post_id, comment_id, db=db)
