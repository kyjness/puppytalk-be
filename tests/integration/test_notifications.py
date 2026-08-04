"""notifications 통합: keyset CursorPage 목록(ADR 0002)·전체 읽음. 라이브 PG 필요(없으면 collect)."""

import pytest
from app.common.enums import NotificationKind
from app.domain.notifications.model import NotificationsRepository
from app.domain.users.model import User
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


def _token(res_json: dict) -> str:
    d = res_json.get("data", res_json)
    t = d.get("accessToken") or d.get("access_token")
    if not t:
        raise AssertionError("로그인 응답에 accessToken이 없습니다.")
    return t


async def _auth(client: AsyncClient, email: str, nickname: str) -> dict[str, str]:
    pw = "TestPassword123!"
    await client.post(
        "/v1/auth/signup", json={"email": email, "password": pw, "nickname": nickname}
    )
    res = await client.post("/v1/auth/login", json={"email": email, "password": pw})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {_token(res.json())}"}


async def _uid(db: AsyncSession, email: str):
    return (await db.execute(select(User.id).where(User.email == email))).scalar_one()


async def _seed(db: AsyncSession, user_id, n: int) -> None:
    for _ in range(n):
        await NotificationsRepository.insert(
            user_id=user_id,
            kind=NotificationKind.LIKE_POST,
            actor_id=None,
            post_id=None,
            comment_id=None,
            db=db,
        )
    await db.commit()


async def test_list_notifications_keyset_and_no_total(
    client: AsyncClient, db_session: AsyncSession
):
    h = await _auth(client, "notif_a@example.com", "알림A")
    uid = await _uid(db_session, "notif_a@example.com")
    await _seed(db_session, uid, 3)

    p1 = await client.get("/v1/notifications", params={"size": 2}, headers=h)
    assert p1.status_code == 200, p1.text
    d1 = p1.json()["data"]
    assert "total" not in d1  # CursorPage — ADR 0002는 total을 노출하지 않는다.
    assert len(d1["items"]) == 2
    assert d1["hasMore"] is True

    cursor = d1["items"][-1]["id"]
    p2 = await client.get("/v1/notifications", params={"size": 2, "cursor": cursor}, headers=h)
    d2 = p2.json()["data"]
    assert len(d2["items"]) == 1
    assert d2["hasMore"] is False
    # 페이지 간 중복 없음(keyset 전진).
    assert d2["items"][0]["id"] not in {i["id"] for i in d1["items"]}


async def test_list_notifications_requires_auth(client: AsyncClient):
    assert (await client.get("/v1/notifications")).status_code == 401


async def test_mark_all_read_idempotent(client: AsyncClient, db_session: AsyncSession):
    h = await _auth(client, "notif_b@example.com", "알림B")
    uid = await _uid(db_session, "notif_b@example.com")
    await _seed(db_session, uid, 2)

    first = await client.patch("/v1/notifications/read", json={}, headers=h)
    assert first.status_code == 200, first.text
    assert first.json()["data"]["updatedCount"] == 2
    # 이미 읽음 → 재요청은 0건.
    again = await client.patch("/v1/notifications/read", json={}, headers=h)
    assert again.json()["data"]["updatedCount"] == 0
