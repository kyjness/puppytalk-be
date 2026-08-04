"""차단 목록 커서 페이지네이션 통합 테스트.

목록 규약(ADR 0002)은 cursor인데 차단 목록만 전량 반환이었다. 정렬·커서 축은
blocked_id — user_blocks의 PK가 (blocker_id, blocked_id)라 추가 인덱스 없이 커버된다.
실 Postgres(TEST_DB_URL) 필요.
"""

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

_PW = "BlockTestPW123!"


def _auth_header(login_json: dict) -> dict[str, str]:
    data = login_json.get("data", login_json)
    token = data.get("accessToken") or data.get("access_token")
    assert token
    return {"Authorization": f"Bearer {token}"}


async def _signup_login(client: AsyncClient, email: str, nickname: str) -> dict[str, str]:
    await client.post(
        "/v1/auth/signup", json={"email": email, "password": _PW, "nickname": nickname}
    )
    res = await client.post("/v1/auth/login", json={"email": email, "password": _PW})
    assert res.status_code == 200, res.text
    return _auth_header(res.json())


async def _signup_get_id(client: AsyncClient, email: str, nickname: str) -> str:
    """가입 후 그 유저의 공개 id를 얻는다(프로필 조회로 확보)."""
    headers = await _signup_login(client, email, nickname)
    res = await client.get("/v1/users/me", headers=headers)
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    return data["id"]


async def _blocks(client: AsyncClient, headers, *, size=20, cursor=None) -> dict:
    params: dict = {"size": size}
    if cursor is not None:
        params["cursor"] = cursor
    res = await client.get("/v1/users/me/blocks", params=params, headers=headers)
    assert res.status_code == 200, res.text
    return res.json()["data"]


async def test_blocked_list_paginates_without_overlap(client: AsyncClient):
    blocker = await _signup_login(client, "blk_owner@example.com", "차단주인")
    targets = [
        await _signup_get_id(client, f"blk_t{i}@example.com", f"차단대상{i}") for i in range(5)
    ]
    for tid in targets:
        res = await client.post(f"/v1/users/{tid}/block", headers=blocker)
        assert res.status_code == 200, res.text

    page1 = await _blocks(client, blocker, size=2)
    assert page1["hasMore"] is True
    assert len(page1["items"]) == 2

    page2 = await _blocks(client, blocker, size=2, cursor=page1["items"][-1]["id"])
    assert page2["hasMore"] is True
    assert len(page2["items"]) == 2

    page3 = await _blocks(client, blocker, size=2, cursor=page2["items"][-1]["id"])
    assert page3["hasMore"] is False
    assert len(page3["items"]) == 1

    seen = [it["id"] for p in (page1, page2, page3) for it in p["items"]]
    assert len(seen) == len(set(seen)) == 5  # 중복·누락 없음
    assert set(seen) == set(targets)
    assert seen == sorted(seen, reverse=True)  # blocked_id DESC


async def test_unblock_removes_from_list(client: AsyncClient):
    blocker = await _signup_login(client, "blk_un@example.com", "해제주인")
    target = await _signup_get_id(client, "blk_unt@example.com", "해제대상")

    await client.post(f"/v1/users/{target}/block", headers=blocker)
    assert [it["id"] for it in (await _blocks(client, blocker))["items"]] == [target]

    await client.post(f"/v1/users/{target}/block", headers=blocker)  # 토글 = 해제
    assert (await _blocks(client, blocker))["items"] == []


async def test_empty_block_list_has_no_more(client: AsyncClient):
    headers = await _signup_login(client, "blk_empty@example.com", "빈차단")

    data = await _blocks(client, headers)
    assert data["items"] == []
    assert data["hasMore"] is False
