"""관리자 신고 피드(#5) — DB 없이 검증하는 회귀 테스트.

인메모리 병합·슬라이스·cap 제거와 DB-side UNION ALL 페이지네이션(ADR 0012)을 소스·컴파일
수준에서 확인한다.
"""

import inspect

from app.common.enums import TargetType
from app.domain.admin.service import AdminService
from app.domain.comments.model import Comment
from app.domain.comments.repository import CommentsRepository
from app.domain.posts.model import Post
from app.domain.posts.repository import PostsRepository
from app.domain.reports.model import Report
from sqlalchemy import func, literal, select
from sqlalchemy.dialects import postgresql


def test_get_reported_posts_has_no_inmemory_pagination():
    # #5 회귀 방지: 인메모리 병합·정렬·슬라이스·500 cap·동일 세션 concurrent gather가 없어야 한다.
    src = inspect.getsource(AdminService.get_reported_posts)
    assert ".sort(" not in src
    assert "merged[" not in src
    assert "min(500" not in src
    assert "asyncio" not in src
    # UNION 페이지가 정한 (type, id) 순서로만 조립한다.
    assert "AdminReportsRepository.page_reported_targets" in src


def test_page_reported_targets_compiles_to_union_all_offset():
    # UNION ALL + 단일 ORDER BY + LIMIT/OFFSET, 그리고 union 위 count(*).
    post_sel = select(
        literal(TargetType.POST.value).label("target_type"),
        Post.id.label("target_id"),
        Post.report_count.label("rc"),
        Post.created_at.label("ca"),
    ).where(Post.deleted_at.is_(None), Post.report_count > 0, Post.user_id.isnot(None))
    comment_sel = select(
        literal(TargetType.COMMENT.value).label("target_type"),
        Comment.id.label("target_id"),
        Comment.report_count.label("rc"),
        Comment.created_at.label("ca"),
    ).where(Comment.deleted_at.is_(None), Comment.report_count > 0, Comment.author_id.isnot(None))
    u = post_sel.union_all(comment_sel).subquery()
    page = (
        select(u.c.target_type, u.c.target_id)
        .order_by(u.c.rc.desc(), u.c.ca.desc(), u.c.target_id.desc())
        .limit(20)
        .offset(40)
    )
    count = select(func.count()).select_from(u)
    page_sql = str(page.compile(dialect=postgresql.dialect()))
    count_sql = str(count.compile(dialect=postgresql.dialect()))
    assert "UNION ALL" in page_sql
    assert page_sql.count("ORDER BY") == 1
    assert "LIMIT" in page_sql and "OFFSET" in page_sql
    assert "count(*)" in count_sql and "UNION ALL" in count_sql


def test_reports_target_index_present():
    names = {i.name for i in Report.metadata.tables["reports"].indexes}
    assert "ix_reports_target" in names  # (target_type, target_id) 부분 인덱스


def test_offset_reported_loaders_replaced_by_ids():
    # 관리자 단독 호출이던 offset 로더는 하이드레이션용 by-ids 로더로 대체됐다.
    assert not hasattr(PostsRepository, "get_reported_posts")
    assert not hasattr(CommentsRepository, "get_reported_comments")
    assert hasattr(PostsRepository, "get_reported_by_ids")
    assert hasattr(CommentsRepository, "get_reported_by_ids")
