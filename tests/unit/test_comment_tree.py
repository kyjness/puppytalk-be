"""댓글 트리 조립 단위 테스트.

Unit B에서 루트는 keyset(DB)로 순서가 확정되고, 트리 조립(_build_comment_tree)은
순수 Python이 됐다. DB 없이 조립·정렬·삭제 placeholder·is_liked 매핑을 결정적으로 검증한다.

대댓글은 이제 **preview**로 들어온다 — 입력은 (대댓글, 그 부모의 총 표시 가능 개수) 쌍이고,
조립은 그 총 개수로 reply_count·has_more_replies를 채운다.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.domain.comments.service import _build_comment_tree, _comment_to_response

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _row(*, parent_id=None, like_count=0, deleted=False, edited=False, content="c"):
    cid = uuid.uuid4()
    return SimpleNamespace(
        id=cid,
        parent_id=parent_id,
        content=content,
        author=None,
        created_at=_T0,
        updated_at=_T0 + timedelta(minutes=5) if edited else _T0,
        post_id=uuid.uuid4(),
        like_count=like_count,
        deleted_at=_T0 if deleted else None,
    )


def _previews(rows, total=None):
    """(대댓글, 부모 총 개수) 쌍으로 감싼다. total 미지정이면 preview가 전부인 상황."""
    return [(r, total if total is not None else len(rows)) for r in rows]


def test_replies_attach_under_correct_root():
    r1, r2 = _row(), _row()
    a = _row(parent_id=r1.id)
    b = _row(parent_id=r1.id)
    c = _row(parent_id=r2.id)
    tree = _build_comment_tree([r1, r2], [(a, 2), (b, 2), (c, 1)], liked_ids=set())
    assert [t.id for t in tree] == [r1.id, r2.id]  # 루트 순서 보존
    assert {rp.id for rp in tree[0].replies} == {a.id, b.id}
    assert [rp.id for rp in tree[1].replies] == [c.id]


def test_root_order_preserved_replies_always_newest_first():
    # 루트는 DB가 정한 입력 순서 그대로, 대댓글은 정렬 옵션과 무관하게 항상 최신순(id DESC).
    # 대댓글엔 좋아요 UI가 없어 인기순이 성립하지 않고, 등록순은 계약에서 빠졌다(ADR 0016).
    r = _row()
    reps = [_row(parent_id=r.id) for _ in range(3)]
    ids_asc = sorted(rp.id for rp in reps)

    tree = _build_comment_tree([r], _previews(reps), liked_ids=set())
    assert [rp.id for rp in tree[0].replies] == list(reversed(ids_asc))


def test_deleted_root_renders_placeholder():
    r = _row(deleted=True, content="원문")
    child = _row(parent_id=r.id, content="대댓글은 유지")
    tree = _build_comment_tree([r], [(child, 1)], liked_ids=set())
    assert tree[0].is_deleted is True
    assert tree[0].content == "삭제된 댓글입니다."
    assert tree[0].replies[0].content == "대댓글은 유지"


def test_is_liked_driven_by_liked_ids():
    r = _row()
    child = _row(parent_id=r.id)
    tree = _build_comment_tree([r], [(child, 1)], liked_ids={child.id})
    assert tree[0].is_liked is False
    assert tree[0].replies[0].is_liked is True


def test_reply_with_unmatched_parent_is_dropped():
    # 저장소가 부모∈roots를 보장하지만, 방어적으로 미매칭 대댓글은 조용히 버린다.
    r = _row()
    orphan = _row(parent_id=uuid.uuid4())
    tree = _build_comment_tree([r], [(orphan, 1)], liked_ids=set())
    assert tree[0].replies == []
    assert len(tree) == 1


def test_reply_count_and_has_more_reflect_total_not_preview_size():
    """preview는 잘려 있어도 reply_count는 전체 개수여야 한다 — FE가 '더보기'를 띄우는 근거."""
    r = _row()
    preview = [_row(parent_id=r.id) for _ in range(3)]
    tree = _build_comment_tree([r], _previews(preview, total=17), liked_ids=set())

    assert len(tree[0].replies) == 3
    assert tree[0].reply_count == 17
    assert tree[0].has_more_replies is True


def test_no_more_replies_when_preview_covers_all():
    r = _row()
    preview = [_row(parent_id=r.id) for _ in range(2)]
    tree = _build_comment_tree([r], _previews(preview, total=2), liked_ids=set())

    assert tree[0].reply_count == 2
    assert tree[0].has_more_replies is False


def test_root_without_replies_reports_zero():
    r = _row()
    tree = _build_comment_tree([r], [], liked_ids=set())

    assert tree[0].replies == []
    assert tree[0].reply_count == 0
    assert tree[0].has_more_replies is False


def test_comment_to_response_is_edited_flag():
    assert _comment_to_response(_row(edited=True), set()).is_edited is True
    assert _comment_to_response(_row(edited=False), set()).is_edited is False
