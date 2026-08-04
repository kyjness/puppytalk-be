"""댓글 목록 keyset 트리 페이지네이션 통합 테스트.

루트 keyset + 대댓글 preview(#6·#21)를 API 레벨로 검증한다: 트리 조립, has_more/cursor 전진,
삭제 루트 placeholder 시맨틱, 목록 is_liked, 그리고 preview 상한과 "더보기" 페이지네이션.
실 Postgres(TEST_DB_URL) 필요.
"""

import pytest
from app.core.ids import new_ulid_str
from app.domain.comments.service import REPLY_PREVIEW_LIMIT
from httpx import AsyncClient

from tests.integration.conftest import signup_login

pytestmark = pytest.mark.asyncio

_PW = "CommentTestPW123!"


async def _signup_login(client: AsyncClient, email: str, nickname: str) -> dict[str, str]:
    return await signup_login(client, email, nickname, password=_PW)


def _data_id(res) -> str:
    body = res.json()
    cid = body.get("data", {}).get("id") or body.get("id")
    assert cid, res.text
    return cid


async def _create_post(client: AsyncClient, headers: dict[str, str]) -> str:
    res = await client.post(
        "/v1/posts",
        json={"title": "댓글 테스트", "content": "본문"},
        headers={**headers, "X-Idempotency-Key": new_ulid_str()},
    )
    assert res.status_code == 201, res.text
    return _data_id(res)


async def _add_comment(
    client: AsyncClient, headers: dict[str, str], post_id: str, content: str, parent_id=None
) -> str:
    payload: dict = {"content": content}
    if parent_id is not None:
        payload["parent_id"] = parent_id
    res = await client.post(f"/v1/posts/{post_id}/comments", json=payload, headers=headers)
    assert res.status_code == 201, res.text
    return _data_id(res)


async def _list(client: AsyncClient, headers, post_id, *, size=10, cursor=None, sort=None) -> dict:
    params: dict = {"size": size}
    if cursor is not None:
        params["cursor"] = cursor
    if sort is not None:
        params["sort"] = sort
    res = await client.get(f"/v1/posts/{post_id}/comments", params=params, headers=headers)
    assert res.status_code == 200, res.text
    return res.json()["data"]


async def test_list_builds_root_with_replies(client: AsyncClient):
    headers = await _signup_login(client, "c_tree@example.com", "트리퍼피")
    post_id = await _create_post(client, headers)
    root_id = await _add_comment(client, headers, post_id, "루트")
    await _add_comment(client, headers, post_id, "대댓글1", parent_id=root_id)
    await _add_comment(client, headers, post_id, "대댓글2", parent_id=root_id)

    data = await _list(client, headers, post_id)
    assert data["hasMore"] is False
    assert len(data["items"]) == 1
    root = data["items"][0]
    assert root["id"] == root_id
    assert len(root["replies"]) == 2


async def test_keyset_pagination_over_roots(client: AsyncClient):
    headers = await _signup_login(client, "c_page@example.com", "페이지퍼피")
    post_id = await _create_post(client, headers)
    created = [await _add_comment(client, headers, post_id, f"루트{i}") for i in range(3)]

    page1 = await _list(client, headers, post_id, size=2, sort="latest")
    assert page1["hasMore"] is True
    assert len(page1["items"]) == 2
    # latest = 최신(마지막 생성)부터
    assert [it["id"] for it in page1["items"]] == [created[2], created[1]]

    cursor = page1["items"][-1]["id"]
    page2 = await _list(client, headers, post_id, size=2, cursor=cursor, sort="latest")
    assert page2["hasMore"] is False
    assert [it["id"] for it in page2["items"]] == [created[0]]


async def test_deleted_root_with_reply_kept_as_placeholder(client: AsyncClient):
    headers = await _signup_login(client, "c_del1@example.com", "삭제퍼피1")
    post_id = await _create_post(client, headers)
    root_id = await _add_comment(client, headers, post_id, "삭제될 루트")
    await _add_comment(client, headers, post_id, "살아남을 대댓글", parent_id=root_id)

    del_res = await client.delete(f"/v1/posts/{post_id}/comments/{root_id}", headers=headers)
    assert del_res.status_code == 200, del_res.text

    data = await _list(client, headers, post_id)
    assert len(data["items"]) == 1
    root = data["items"][0]
    assert root["isDeleted"] is True
    assert root["content"] == "삭제된 댓글입니다."
    assert len(root["replies"]) == 1


async def test_deleted_root_without_reply_hidden(client: AsyncClient):
    headers = await _signup_login(client, "c_del2@example.com", "삭제퍼피2")
    post_id = await _create_post(client, headers)
    root_id = await _add_comment(client, headers, post_id, "자식 없는 루트")

    await client.delete(f"/v1/posts/{post_id}/comments/{root_id}", headers=headers)

    data = await _list(client, headers, post_id)
    assert data["items"] == []
    assert data["hasMore"] is False


async def test_list_reflects_is_liked(client: AsyncClient):
    headers = await _signup_login(client, "c_like@example.com", "좋아요퍼피")
    post_id = await _create_post(client, headers)
    liked = await _add_comment(client, headers, post_id, "좋아요할 댓글")
    unliked = await _add_comment(client, headers, post_id, "안 할 댓글")

    like_res = await client.post(f"/v1/likes/comments/{liked}", headers=headers)
    assert like_res.status_code == 200, like_res.text

    data = await _list(client, headers, post_id)
    by_id = {it["id"]: it for it in data["items"]}
    assert by_id[liked]["isLiked"] is True
    assert by_id[liked]["likeCount"] == 1
    assert by_id[unliked]["isLiked"] is False


async def test_double_like_comment_does_not_double_count(client: AsyncClient):
    # #15: 카운터를 CommentsRepository로 일원화한 뒤에도 연타 좋아요가 이중 집계되지 않아야 한다.
    headers = await _signup_login(client, "c_dbl@example.com", "연타퍼피")
    post_id = await _create_post(client, headers)
    cid = await _add_comment(client, headers, post_id, "연타 대상")

    await client.post(f"/v1/likes/comments/{cid}", headers=headers)
    await client.post(f"/v1/likes/comments/{cid}", headers=headers)  # 재요청(멱등)

    data = await _list(client, headers, post_id)
    assert data["items"][0]["likeCount"] == 1


async def test_popular_sort_removed_falls_back_gracefully(client: AsyncClient):
    # 인기순은 제거됐다. sort=popular는 에러 없이 기본(latest)로 동작해야 한다.
    headers = await _signup_login(client, "c_pop@example.com", "인기퍼피")
    post_id = await _create_post(client, headers)
    await _add_comment(client, headers, post_id, "댓글")

    data = await _list(client, headers, post_id, sort="popular")
    assert len(data["items"]) == 1


# --- 대댓글 preview + 더보기 (#21) ---
# 목록 응답은 루트당 REPLY_PREVIEW_LIMIT건만 싣는다 — 인기 스레드의 루트 하나에 대댓글이
# 수천 건이면 한 응답이 그만큼을 끌어오던 문제(운영 봉투의 핫스팟 전제와 배치)를 막는다.


async def _replies(client: AsyncClient, headers, post_id, comment_id, *, size=10, cursor=None):
    params: dict = {"size": size}
    if cursor is not None:
        params["cursor"] = cursor
    res = await client.get(
        f"/v1/posts/{post_id}/comments/{comment_id}/replies", params=params, headers=headers
    )
    assert res.status_code == 200, res.text
    return res.json()["data"]


async def test_list_caps_replies_to_preview_with_accurate_count(client: AsyncClient):
    headers = await _signup_login(client, "c_prev@example.com", "프리뷰퍼피")
    post_id = await _create_post(client, headers)
    root_id = await _add_comment(client, headers, post_id, "루트")
    for i in range(8):
        await _add_comment(client, headers, post_id, f"대댓글{i}", parent_id=root_id)

    root = (await _list(client, headers, post_id))["items"][0]
    assert len(root["replies"]) == REPLY_PREVIEW_LIMIT  # 8건이 아니라 preview만
    assert root["replyCount"] == 8  # 총 개수는 정확
    assert root["hasMoreReplies"] is True


async def test_replies_endpoint_paginates_without_overlap(client: AsyncClient):
    headers = await _signup_login(client, "c_more@example.com", "더보기퍼피")
    post_id = await _create_post(client, headers)
    root_id = await _add_comment(client, headers, post_id, "루트")
    created = [
        await _add_comment(client, headers, post_id, f"대댓글{i}", parent_id=root_id)
        for i in range(5)
    ]

    page1 = await _replies(client, headers, post_id, root_id, size=2)
    assert page1["hasMore"] is True
    assert [it["id"] for it in page1["items"]] == [created[4], created[3]]  # latest 기본

    page2 = await _replies(client, headers, post_id, root_id, size=2, cursor=created[3])
    assert page2["hasMore"] is True
    assert [it["id"] for it in page2["items"]] == [created[2], created[1]]

    page3 = await _replies(client, headers, post_id, root_id, size=2, cursor=created[1])
    assert page3["hasMore"] is False
    assert [it["id"] for it in page3["items"]] == [created[0]]

    seen = [it["id"] for p in (page1, page2, page3) for it in p["items"]]
    assert len(seen) == len(set(seen)) == 5  # 중복·누락 없음


async def test_replies_endpoint_excludes_deleted_and_counts_match(client: AsyncClient):
    headers = await _signup_login(client, "c_mdel@example.com", "더보기삭제퍼피")
    post_id = await _create_post(client, headers)
    root_id = await _add_comment(client, headers, post_id, "루트")
    ids = [
        await _add_comment(client, headers, post_id, f"대댓글{i}", parent_id=root_id)
        for i in range(4)
    ]

    res = await client.delete(f"/v1/posts/{post_id}/comments/{ids[0]}", headers=headers)
    assert res.status_code == 200, res.text

    page = await _replies(client, headers, post_id, root_id, size=10)
    assert ids[0] not in [it["id"] for it in page["items"]]
    assert len(page["items"]) == 3
    # 목록의 replyCount도 같은 가시성 규칙을 따라야 한다.
    root = (await _list(client, headers, post_id))["items"][0]
    assert root["replyCount"] == 3


async def test_replies_endpoint_rejects_non_root_and_foreign_ids(client: AsyncClient):
    headers = await _signup_login(client, "c_m404@example.com", "더보기404퍼피")
    post_id = await _create_post(client, headers)
    root_id = await _add_comment(client, headers, post_id, "루트")
    reply_id = await _add_comment(client, headers, post_id, "대댓글", parent_id=root_id)

    # 대댓글 id로는 더 파고들 수 없다(2단 구조 유지).
    res = await client.get(f"/v1/posts/{post_id}/comments/{reply_id}/replies", headers=headers)
    assert res.status_code == 404, res.text

    # 다른 게시글의 루트 id 조합도 거부.
    other_post = await _create_post(client, headers)
    res = await client.get(f"/v1/posts/{other_post}/comments/{root_id}/replies", headers=headers)
    assert res.status_code == 404, res.text
