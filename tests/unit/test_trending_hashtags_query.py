"""트렌딩 해시태그(멍태그) 집계 기준 단위 테스트.

"지금 뜨는"이려면 집계 창이 있어야 한다 — 창이 없으면 전체 기간 누적이라 순위가
시간이 갈수록 고정된다. 창은 트렌딩 게시글과 같은 서버 고정 24h(ADR 0004)이고,
데이터가 희소하면 전체 기간으로 폴백해 빈 위젯을 막는다.
"""

import asyncio
from typing import cast

from app.domain.posts.repository import PostsRepository
from app.domain.posts.services.hashtag_service import (
    _MIN_HASHTAGS_FOR_WINDOW,
    _WINDOW_HOURS,
    HashtagService,
)
from sqlalchemy.ext.asyncio import AsyncSession

from tests.unit.fakes import FakeDB


def _compiled(**kwargs) -> str:
    stmt = PostsRepository.get_trending_hashtags_query(**kwargs)
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def test_query_applies_aggregation_window():
    compiled = _compiled(window_hours=24, limit=10)
    assert "created_at >=" in compiled
    assert "deleted_at IS NULL" in compiled
    assert "is_blinded IS false" in compiled


def test_query_has_deterministic_tie_breaker():
    """동점 태그의 순서가 비결정적이면 캐시 갱신마다 목록이 뒤집혀 보인다."""
    compiled = _compiled(window_hours=24, limit=10)
    assert "ORDER BY count DESC, hashtags.name ASC" in compiled


def test_query_without_window_has_no_time_filter():
    """폴백 경로(window_hours=None)는 기간 제한이 없어야 한다."""
    compiled = _compiled(window_hours=None, limit=10)
    assert "created_at >=" not in compiled


def _run_loader(monkeypatch, rows_by_window: dict[int | None, list[tuple[str, int]]]):
    """repository를 가짜로 갈아끼우고 서비스 loader를 태운 뒤 (결과, 호출된 창) 반환."""
    calls: list[int | None] = []

    async def _fake(cls, *, db, window_hours=24, limit=10):
        calls.append(window_hours)
        return rows_by_window.get(window_hours, [])

    monkeypatch.setattr(PostsRepository, "get_trending_hashtags", classmethod(_fake))

    result = asyncio.run(
        HashtagService.get_trending_hashtags(
            db=cast(AsyncSession, FakeDB()), redis_client=None, limit=10
        )
    )
    return result, calls


def test_sparse_window_falls_back_to_all_time(monkeypatch):
    sparse = [("산책", 1)]  # _MIN_HASHTAGS_FOR_WINDOW(3) 미만
    assert len(sparse) < _MIN_HASHTAGS_FOR_WINDOW
    all_time = [("산책", 9), ("간식", 7), ("목욕", 5)]

    result, calls = _run_loader(monkeypatch, {_WINDOW_HOURS: sparse, None: all_time})

    assert calls == [_WINDOW_HOURS, None]  # 24h 먼저, 부족하면 전체 기간
    assert [r.name for r in result] == ["산책", "간식", "목욕"]


def test_sufficient_window_does_not_fall_back(monkeypatch):
    rows = [("산책", 4), ("간식", 3), ("목욕", 2)]

    result, calls = _run_loader(monkeypatch, {_WINDOW_HOURS: rows})

    assert calls == [_WINDOW_HOURS]  # 폴백 재조회 없음
    assert [r.count for r in result] == [4, 3, 2]
