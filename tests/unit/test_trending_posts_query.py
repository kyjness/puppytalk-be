from app.domain.posts.repository import PostsRepository


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
    assert "posts.like_count * 2" in compiled
    assert "posts.view_count * 0.1" in compiled
    # 댓글은 보조 신호(가중치 1). 좋아요 가중치를 넘어서면 랭킹 성격이 뒤집힌다.
    assert "posts.comment_count * 1" in compiled


def test_trending_posts_query_decay_exponent_is_not_double_counted():
    """집계 창이 이미 1차 감쇠라 지수는 1.0 — 이중 감쇠(1.3)로 되돌아가면 실패."""
    compiled = _compiled(use_time_decay=True, limit=10)
    assert "+ 2, 1.0)" in compiled


def test_trending_posts_query_fallback_order_clause():
    compiled = _compiled(use_time_decay=False, limit=5)
    assert "like_count" in compiled
    assert "power" not in compiled.lower()
