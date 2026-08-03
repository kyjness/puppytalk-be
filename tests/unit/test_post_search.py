import pytest
from app.common.exceptions import InvalidRequestException
from app.domain.posts.repository import ParsedSearch, PostsModel, validate_search_query


def test_validate_search_query_splits_whitespace_tokens():
    assert validate_search_query("불닭  레시피") == ParsedSearch(
        tag=None, tokens=("불닭", "레시피")
    )


def test_validate_search_query_rejects_short_token():
    with pytest.raises(InvalidRequestException):
        validate_search_query("불")
    with pytest.raises(InvalidRequestException):
        validate_search_query("ab")
    with pytest.raises(InvalidRequestException):
        validate_search_query("1")
    assert validate_search_query("불닭") == ParsedSearch(tag=None, tokens=("불닭",))
    assert validate_search_query("abc") == ParsedSearch(tag=None, tokens=("abc",))
    assert validate_search_query("12") == ParsedSearch(tag=None, tokens=("12",))
    assert validate_search_query("2024") == ParsedSearch(tag=None, tokens=("2024",))


def test_validate_search_query_hashtag_bypasses_min_len_and_normalizes():
    # #태그는 길이 정책 미적용 + 소문자 정규화. 파싱은 여기서 끝나 필터는 재파싱하지 않는다.
    assert validate_search_query("#ab") == ParsedSearch(tag="ab", tokens=())
    assert validate_search_query("#DogFood") == ParsedSearch(tag="dogfood", tokens=())


def test_validate_search_query_empty_returns_none():
    assert validate_search_query(None) is None
    assert validate_search_query("   ") is None


def test_posts_model_exposes_post_is_visible():
    assert hasattr(PostsModel, "post_is_visible")
    assert callable(PostsModel.post_is_visible)
