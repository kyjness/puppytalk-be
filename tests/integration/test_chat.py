"""chat 통합: 인박스 미읽음 집계(#16)·방 접근 멤버십 가드(#19). 라이브 PG 필요(없으면 collect)."""

from datetime import timedelta

import pytest
from app.core.ids import uuid_to_base62
from app.db.base_class import utc_now
from app.domain.chat.model import ChatMessage, ChatRoom, normalize_dm_user_ids
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


async def _make_room(db: AsyncSession, a, b) -> ChatRoom:
    u1, u2 = normalize_dm_user_ids(a, b)
    now = utc_now()
    room = ChatRoom(user1_id=u1, user2_id=u2, created_at=now, updated_at=now)
    db.add(room)
    await db.flush()
    return room


async def test_inbox_unread_counts_peer_messages_only(
    client: AsyncClient, db_session: AsyncSession
):
    a = await _auth(client, "chat_a@example.com", "채팅A")
    await _auth(client, "chat_b@example.com", "채팅B")
    aid = await _uid(db_session, "chat_a@example.com")
    bid = await _uid(db_session, "chat_b@example.com")

    room = await _make_room(db_session, aid, bid)
    now = utc_now()
    # 상대(B)가 보낸 미읽음 2건 + 내(A)가 보낸 1건. 미읽음은 상대 발신분만 세어야 한다.
    db_session.add_all(
        [
            ChatMessage(
                room_id=room.id, sender_id=bid, content="hi1", is_read=False, created_at=now
            ),
            ChatMessage(
                room_id=room.id, sender_id=bid, content="hi2", is_read=False, created_at=now
            ),
            ChatMessage(
                room_id=room.id, sender_id=aid, content="yo", is_read=False, created_at=now
            ),
        ]
    )
    await db_session.commit()

    res = await client.get("/v1/chat/rooms", headers=a)
    assert res.status_code == 200, res.text
    items = res.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["unreadCount"] == 2

    read = await client.post(f"/v1/chat/rooms/{items[0]['roomId']}/read", headers=a)
    assert read.status_code == 200, read.text
    after = await client.get("/v1/chat/rooms", headers=a)
    assert after.json()["data"]["items"][0]["unreadCount"] == 0


async def test_room_access_guarded_by_membership(client: AsyncClient, db_session: AsyncSession):
    a = await _auth(client, "peer_a@example.com", "피어A")
    await _auth(client, "peer_b@example.com", "피어B")
    c = await _auth(client, "peer_c@example.com", "피어C")
    aid = await _uid(db_session, "peer_a@example.com")
    bid = await _uid(db_session, "peer_b@example.com")

    room = await _make_room(db_session, aid, bid)
    rid = room.id
    await db_session.commit()
    pub = uuid_to_base62(rid)

    # 멤버(A)는 200이고 상대 정보 = B, 비멤버(C)는 방 정보·메시지 모두 403.
    ok = await client.get(f"/v1/chat/rooms/{pub}", headers=a)
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["peerNickname"] == "피어B"

    assert (await client.get(f"/v1/chat/rooms/{pub}", headers=c)).status_code == 403
    assert (await client.get(f"/v1/chat/rooms/{pub}/messages", headers=c)).status_code == 403


async def test_messages_after_direction_walks_forward_without_gaps(
    client: AsyncClient, db_session: AsyncSession
):
    """재연결 재동기(direction=after)를 라이브 PG로 검증한다.

    복합 커서 `(created_at, id) > (…)` + ASC 정렬은 실제 DB에서만 확인되는 부분이고,
    이 경로의 계약은 "끊긴 구간을 빠짐없이 잇는다"라 페이지 경계에서 한 건이라도
    빠지거나 겹치면 안 된다.
    """
    a = await _auth(client, "gap_a@example.com", "갭A")
    await _auth(client, "gap_b@example.com", "갭B")
    aid = await _uid(db_session, "gap_a@example.com")
    bid = await _uid(db_session, "gap_b@example.com")

    room = await _make_room(db_session, aid, bid)
    base = utc_now()
    # m0(= 내가 가진 마지막 메시지) 이후로 5건이 쌓인 상태.
    msgs = [
        ChatMessage(
            room_id=room.id,
            sender_id=bid,
            content=f"m{i}",
            is_read=False,
            created_at=base + timedelta(minutes=i),
        )
        for i in range(6)
    ]
    db_session.add_all(msgs)
    await db_session.flush()
    ids = [uuid_to_base62(m.id) for m in msgs]  # 커밋 전에 확보 — 이후 세션을 건드리지 않는다
    pub = uuid_to_base62(room.id)
    await db_session.commit()

    cursor = ids[0]  # m0 이후를 요청한다

    # limit=2로 이어 받아 m1..m5를 모두 모은다(페이지마다 다음 커서는 items[0]).
    collected: list[str] = []
    for _ in range(5):
        res = await client.get(
            f"/v1/chat/rooms/{pub}/messages",
            headers=a,
            params={"cursor": cursor, "limit": 2, "direction": "after"},
        )
        assert res.status_code == 200, res.text
        data = res.json()["data"]
        items = data["items"]
        assert items, "after 페이지가 비면 안 된다"
        # 응답 계약: 방향과 무관하게 항상 최신순
        assert [i["content"] for i in items] == sorted([i["content"] for i in items], reverse=True)
        collected.extend(i["content"] for i in reversed(items))
        if not data["hasMore"]:
            break
        cursor = items[0]["id"]

    # 빠짐도 겹침도 없어야 한다 — 최신 N건만 다시 읽는 방식이면 중간이 빈다.
    assert collected == ["m1", "m2", "m3", "m4", "m5"]

    # before는 반대 방향으로 과거를 준다(기존 무한 스크롤 계약 회귀 확인).
    back = await client.get(
        f"/v1/chat/rooms/{pub}/messages",
        headers=a,
        params={"cursor": ids[3], "limit": 2},
    )
    assert [i["content"] for i in back.json()["data"]["items"]] == ["m2", "m1"]

    # 이을 지점이 없는 after는 거부한다 — 조용히 최신 페이지를 주면 구멍을 못 본다.
    no_cursor = await client.get(
        f"/v1/chat/rooms/{pub}/messages", headers=a, params={"direction": "after"}
    )
    assert no_cursor.status_code == 400, no_cursor.text
