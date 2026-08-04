import uuid

import pytest
from app.core.ids import new_ulid_str
from httpx import AsyncClient

from tests.integration.conftest import auth_header

pytestmark = pytest.mark.asyncio

_TEST_PW = "LikeTestPW123!"


async def setup_post_and_headers(client: AsyncClient) -> tuple[dict[str, str], str]:
    # 통합 스위트는 세션 스코프 스키마를 공유(테스트 간 롤백 없음)하므로 이메일·닉네임을
    # 고유화해 다른 파일과의 닉네임 UNIQUE 충돌(→ signup 409 → login 401)을 막는다.
    suffix = uuid.uuid4().hex[:12]
    payload = {
        "email": f"liker_{suffix}@example.com",
        "password": _TEST_PW,
        "nickname": f"좋아요{suffix[:6]}",
    }
    await client.post("/v1/auth/signup", json=payload)
    login_res = await client.post(
        "/v1/auth/login",
        json={"email": payload["email"], "password": payload["password"]},
    )
    assert login_res.status_code == 200
    headers = auth_header(login_res.json())

    post_res = await client.post(
        "/v1/posts",
        json={"title": "좋아요 테스트", "content": "내용"},
        headers={**headers, "X-Idempotency-Key": new_ulid_str()},
    )
    assert post_res.status_code == 201, post_res.text
    body = post_res.json()
    post_id = body.get("data", {}).get("id") or body.get("id")
    assert post_id
    return headers, post_id


async def test_like_post_then_unlike(client: AsyncClient):
    headers, post_id = await setup_post_and_headers(client)

    like_res = await client.post(f"/v1/likes/posts/{post_id}", headers=headers)
    assert like_res.status_code == 200

    unlike_res = await client.delete(f"/v1/likes/posts/{post_id}", headers=headers)
    assert unlike_res.status_code == 200
