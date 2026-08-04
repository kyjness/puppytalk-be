from app.domain.posts.repository import PostsRepository


def test_trending_posts_query_time_decay_order_clause():
    stmt = PostsRepository.get_trending_posts_query(use_time_decay=True, limit=10)
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "comment_count" in compiled
    assert "like_count" in compiled
    assert "view_count" in compiled
    assert "power" in compiled.lower()
    assert "created_at >=" in compiled


def test_trending_posts_query_fallback_order_clause():
    stmt = PostsRepository.get_trending_posts_query(use_time_decay=False, limit=5)
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "like_count" in compiled
    assert "power" not in compiled.lower()
