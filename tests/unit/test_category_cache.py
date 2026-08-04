"""카테고리 존재 검증 인프로세스 캐시 단위 테스트.

카테고리는 시드 전용이라 TTL 캐시가 안전하지만, 두 가지 함정을 고정한다:
빈 집합(시드 전)은 캐시하지 않고, 테스트 간에는 리셋 헬퍼로 격리한다.
"""

import pytest
from app.domain.posts.services import post_service as ps

from tests.unit.fakes import FakeDB, as_session

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _isolated_cache():
    ps._reset_category_cache()
    yield
    ps._reset_category_cache()


def _patch_ids(monkeypatch, ids_sequence: list[frozenset[int]]):
    """호출마다 ids_sequence를 순서대로 반환(마지막 값 반복). 호출 수 기록."""
    calls: list[int] = []

    async def _get(cls, *, db):
        calls.append(1)
        idx = min(len(calls) - 1, len(ids_sequence) - 1)
        return ids_sequence[idx]

    monkeypatch.setattr(ps.PostsRepository, "get_category_ids", classmethod(_get))
    return calls


async def test_cache_hits_skip_db(monkeypatch):
    calls = _patch_ids(monkeypatch, [frozenset({1, 2})])
    db = as_session(FakeDB())
    assert await ps._category_exists(1, db) is True
    assert await ps._category_exists(2, db) is True
    assert await ps._category_exists(3, db) is False
    assert len(calls) == 1  # TTL 안에서는 DB 1회


async def test_empty_set_is_not_cached(monkeypatch):
    """시드 전 빈 테이블을 캐시하면 시드 후에도 TTL 동안 전부 400 — 빈 집합은 매번 재조회."""
    calls = _patch_ids(monkeypatch, [frozenset(), frozenset({1})])
    db = as_session(FakeDB())
    assert await ps._category_exists(1, db) is False  # 시드 전
    assert await ps._category_exists(1, db) is True  # 시드 직후 즉시 반영
    assert len(calls) == 2


async def test_reset_helper_invalidates(monkeypatch):
    calls = _patch_ids(monkeypatch, [frozenset({1})])
    db = as_session(FakeDB())
    assert await ps._category_exists(1, db) is True
    ps._reset_category_cache()
    assert await ps._category_exists(1, db) is True
    assert len(calls) == 2  # 리셋 후 재조회
