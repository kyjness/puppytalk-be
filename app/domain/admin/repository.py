# 관리자 신고 triage 피드 조회. 신고된 게시글·댓글을 DB-side UNION ALL로 합쳐 정렬·페이지한다.

from uuid import UUID

from sqlalchemy import func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import TargetType
from app.domain.comments.model import Comment
from app.domain.posts.model import Post


class AdminReportsRepository:
    @classmethod
    async def page_reported_targets(
        cls, *, offset: int, size: int, db: AsyncSession
    ) -> tuple[list[tuple[TargetType, UUID]], int]:
        """신고된 게시글·댓글을 하나의 피드로 합쳐 (target_type, id) 페이지 + 총계를 반환.

        두 테이블을 UNION ALL로 합쳐 DB에서 report_count DESC, created_at DESC, id DESC 단일
        정렬·LIMIT/OFFSET 한다. 인메모리 병합·정렬·cap 없이 페이지 경계·total이 정확하다.
        저자가 없는(SET NULL) 신고 콘텐츠는 표시하지 않으므로 total에서도 제외한다.
        """
        post_sel = select(
            literal(TargetType.POST.value).label("target_type"),
            Post.id.label("target_id"),
            Post.report_count.label("rc"),
            Post.created_at.label("ca"),
        ).where(
            Post.deleted_at.is_(None),
            Post.report_count > 0,
            Post.user_id.isnot(None),
        )
        comment_sel = select(
            literal(TargetType.COMMENT.value).label("target_type"),
            Comment.id.label("target_id"),
            Comment.report_count.label("rc"),
            Comment.created_at.label("ca"),
        ).where(
            Comment.deleted_at.is_(None),
            Comment.report_count > 0,
            Comment.author_id.isnot(None),
        )

        u = post_sel.union_all(comment_sel).subquery()
        page_stmt = (
            select(u.c.target_type, u.c.target_id)
            .order_by(u.c.rc.desc(), u.c.ca.desc(), u.c.target_id.desc())
            .limit(size)
            .offset(offset)
        )
        count_stmt = select(func.count()).select_from(u)

        total = (await db.execute(count_stmt)).scalar_one()  # count(*)는 항상 1행
        rows = (await db.execute(page_stmt)).all()
        page = [(TargetType(row[0]), row[1]) for row in rows]  # str 태그 → enum 경계 복원
        return page, total
