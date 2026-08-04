from app.domain.posts.repository import (
    _DECAY_AGE_FLOOR_HOURS,
    _DECAY_EXPONENT,
    _W_COMMENT,
    _W_LIKE,
    _W_VIEW,
    PostsRepository,
)


def _compiled(**kwargs) -> str:
    stmt = PostsRepository.get_trending_posts_query(**kwargs)
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def test_trending_posts_query_time_decay_order_clause():
    compiled = _compiled(use_time_decay=True, limit=10)
    assert "comment_count" in compiled
    assert "like_count" in compiled
    assert "view_count" in compiled
    assert "power" in compiled.lower()
    assert "created_at >=" in compiled


def test_trending_posts_query_weights_are_like_and_view_centric():
    """가중치는 좋아요·조회수 중심 — 상수가 바뀌면 여기서 먼저 드러난다."""
    compiled = _compiled(use_time_decay=True, limit=10)
    # 상수를 그대로 참조한다 — 리터럴을 박으면 손잡이를 돌릴 때마다 테스트를 고쳐야 한다.
    assert f"posts.like_count * {_W_LIKE}" in compiled
    assert f"posts.view_count * {_W_VIEW}" in compiled
    # 댓글은 보조 신호. 좋아요 가중치를 넘어서면 랭킹 성격이 뒤집힌다.
    assert f"posts.comment_count * {_W_COMMENT}" in compiled
    assert _W_COMMENT < _W_LIKE


def test_trending_posts_query_decay_exponent_is_not_double_counted():
    """집계 창이 이미 1차 감쇠라 지수는 1.0 — 이중 감쇠(1.3)로 되돌아가면 실패."""
    compiled = _compiled(use_time_decay=True, limit=10)
    assert f"+ {_DECAY_AGE_FLOOR_HOURS}, {_DECAY_EXPONENT})" in compiled
    # 창(24h)이 이미 1차 감쇠라 지수가 1을 넘으면 이중 감쇠다.
    assert _DECAY_EXPONENT <= 1.0


def test_trending_posts_query_fallback_order_clause():
    compiled = _compiled(use_time_decay=False, limit=5)
    assert "like_count" in compiled
    assert "power" not in compiled.lower()
