import pytest
from app.core.config import settings
from app.core.ids import new_ulid_str
from app.db.base_class import utc_now
from app.domain.comments.model import Comment
from app.domain.posts.model import Post
from app.domain.reports.model import Report
from app.domain.users.model import User
from app.main import app
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.conftest import auth_header

pytestmark = pytest.mark.asyncio

# SignUpRequest: PasswordStr 8~20자
_TEST_PW = "AdminTestPW123!"


async def _admin_headers(client: AsyncClient, db: AsyncSession, email: str, nickname: str) -> dict:
    await client.post(
        "/v1/auth/signup", json={"email": email, "password": _TEST_PW, "nickname": nickname}
    )
    await db.execute(text("UPDATE users SET role = 'ADMIN' WHERE email = :email"), {"email": email})
    await db.commit()
    res = await client.post("/v1/auth/login", json={"email": email, "password": _TEST_PW})
    assert res.status_code == 200, res.text
    return auth_header(res.json())


async def test_admin_access_denied_for_normal_user(client: AsyncClient, db_session: AsyncSession):
    payload = {"email": "normal@example.com", "password": _TEST_PW, "nickname": "일반유저"}
    await client.post("/v1/auth/signup", json=payload)
    login_res = await client.post(
        "/v1/auth/login",
        json={"email": payload["email"], "password": payload["password"]},
    )
    assert login_res.status_code == 200
    headers = auth_header(login_res.json())

    res = await client.get("/v1/admin/reported-posts", headers=headers)
    assert res.status_code == 403


async def test_admin_access_success(client: AsyncClient, db_session: AsyncSession):
    payload = {"email": "admin@example.com", "password": _TEST_PW, "nickname": "관리자"}
    await client.post("/v1/auth/signup", json=payload)

    await db_session.execute(
        text("UPDATE users SET role = 'ADMIN' WHERE email = :email"),
        {"email": payload["email"]},
    )
    await db_session.commit()

    login_res = await client.post(
        "/v1/auth/login",
        json={"email": payload["email"], "password": payload["password"]},
    )
    assert login_res.status_code == 200
    headers = auth_header(login_res.json())

    res = await client.get("/v1/admin/reported-posts", headers=headers)
    assert res.status_code == 200


async def test_suspend_revokes_refresh_token(client: AsyncClient, db_session: AsyncSession):
    """정지되면 기존 refresh 토큰이 무효화된다(#8)."""
    # refresh 회전(RTR)은 Redis 저장소 위에서만 동작한다(refresh_tokens가 redis 없으면 401).
    # 다른 RTR 테스트와 동일하게 Redis 미연결 시 스킵한다.
    if getattr(app.state, "redis", None) is None:
        pytest.skip("Redis 미연결: refresh 토큰 무효화(#8) 검증 생략")
    cookie_name = settings.REFRESH_TOKEN_COOKIE_NAME

    # 대상 유저 가입·로그인 → 공개 id + refresh 쿠키 확보
    target = {"email": "suspend-target@example.com", "password": _TEST_PW, "nickname": "정지대상"}
    await client.post("/v1/auth/signup", json=target)
    login_res = await client.post(
        "/v1/auth/login",
        json={"email": target["email"], "password": target["password"]},
    )
    assert login_res.status_code == 200
    target_id = login_res.json()["data"]["id"]
    refresh_cookie = login_res.cookies.get(cookie_name)
    assert refresh_cookie, "refresh 쿠키 없음"

    # 정지 전에는 refresh 성공 — 이후 401이 '정지 때문'임을 증명(쿠키 자체는 유효)
    client.cookies.clear()
    before = await client.post("/v1/auth/refresh", cookies={cookie_name: refresh_cookie})
    assert before.status_code == 200
    refresh_cookie = before.cookies.get(cookie_name) or refresh_cookie

    # 관리자 생성·승격·로그인
    admin = {"email": "suspender-admin@example.com", "password": _TEST_PW, "nickname": "정지관리자"}
    await client.post("/v1/auth/signup", json=admin)
    await db_session.execute(
        text("UPDATE users SET role = 'ADMIN' WHERE email = :email"),
        {"email": admin["email"]},
    )
    await db_session.commit()
    admin_login = await client.post(
        "/v1/auth/login",
        json={"email": admin["email"], "password": admin["password"]},
    )
    headers = auth_header(admin_login.json())

    # 정지 실행
    suspend_res = await client.patch(f"/v1/admin/users/{target_id}/suspend", headers=headers)
    assert suspend_res.status_code == 200

    # 정지 후에는 기존 refresh 토큰이 무효화되어 401
    client.cookies.clear()
    after = await client.post("/v1/auth/refresh", cookies={cookie_name: refresh_cookie})
    assert after.status_code == 401


async def test_reported_feed_interleaves_and_paginates(
    client: AsyncClient, db_session: AsyncSession
):
    """신고된 게시글·댓글이 report_count DESC 단일 피드로 interleave되고, 페이지 경계가 정확하다(#5).

    공유 DB(테스트 간 정리 없음) 오염과 무관하도록 큰 report_count로 피드 상단을 점유시켜
    상대 순서·페이지 경계·중복 없음을 검증한다.
    """
    headers = await _admin_headers(client, db_session, "feed-admin@example.com", "피드관리자")

    # 콘텐츠 작성자 준비 → id 확보
    await client.post(
        "/v1/auth/signup",
        json={"email": "feed-author@example.com", "password": _TEST_PW, "nickname": "피드작성자"},
    )
    author_id = (
        await db_session.execute(select(User.id).where(User.email == "feed-author@example.com"))
    ).scalar_one()

    now = utc_now()
    host = Post(
        user_id=author_id,
        title="피드 호스트 글",
        content="본문",
        report_count=0,
        created_at=now,
        updated_at=now,
    )
    p_high = Post(
        user_id=author_id,
        title="많이 신고된 글",
        content="P본문",
        report_count=9001,
        created_at=now,
        updated_at=now,
    )
    p_low = Post(
        user_id=author_id,
        title="적게 신고된 글",
        content="P본문2",
        report_count=8998,
        created_at=now,
        updated_at=now,
    )
    db_session.add_all([host, p_high, p_low])
    await db_session.flush()  # host.id 확정(댓글 FK)
    c_high = Comment(
        post_id=host.id,
        author_id=author_id,
        content="많이 신고된 댓글",
        report_count=9000,
        created_at=now,
        updated_at=now,
    )
    c_mid = Comment(
        post_id=host.id,
        author_id=author_id,
        content="중간 댓글",
        report_count=8999,
        created_at=now,
        updated_at=now,
    )
    db_session.add_all([c_high, c_mid])
    await db_session.flush()
    # 집계(last_reported_at·reasons) 경로도 태운다.
    for tt, tid in (("POST", p_high.id), ("COMMENT", c_high.id)):
        db_session.add(
            Report(
                reporter_id=author_id, target_type=tt, target_id=tid, reason="스팸", created_at=now
            )
        )
    await db_session.commit()

    # 상단 4건 = 내가 심은 것, report_count DESC로 interleave: POST, COMMENT, COMMENT, POST
    res = await client.get("/v1/admin/reported-posts?page=1&size=4", headers=headers)
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    top = data["items"][:4]
    assert [i["reportCount"] for i in top] == [9001, 9000, 8999, 8998]
    assert [i["targetType"] for i in top] == ["POST", "COMMENT", "COMMENT", "POST"]
    assert data["total"] >= 4
    # 댓글 항목은 호스트 글 제목을 단다.
    assert top[1]["title"] == "피드 호스트 글"

    # 페이지 경계: size=2 두 페이지가 겹치지 않고 순서가 이어진다(500 cap·인메모리 슬라이스 회귀 방지).
    r1 = (await client.get("/v1/admin/reported-posts?page=1&size=2", headers=headers)).json()[
        "data"
    ]
    r2 = (await client.get("/v1/admin/reported-posts?page=2&size=2", headers=headers)).json()[
        "data"
    ]
    ids1 = [i["id"] for i in r1["items"]]
    ids2 = [i["id"] for i in r2["items"]]
    assert [i["reportCount"] for i in r1["items"]] == [9001, 9000]
    assert [i["reportCount"] for i in r2["items"]] == [8999, 8998]
    assert set(ids1).isdisjoint(ids2)
    assert r1["hasMore"] is True


# --- 블라인드와 comment_count 정합성 ---
# comment_count의 정의는 "표시 가능한(미삭제·미블라인드) 댓글 수"다. 목록이 블라인드 댓글을
# 빼고 내려주므로, 블라인드가 카운트를 건드리지 않으면 "댓글 N개"라고 표시하면서 N-1개만
# 보이는 불일치가 생긴다.


async def _author_headers(client: AsyncClient, email: str, nickname: str) -> dict[str, str]:
    await client.post(
        "/v1/auth/signup", json={"email": email, "password": _TEST_PW, "nickname": nickname}
    )
    res = await client.post("/v1/auth/login", json={"email": email, "password": _TEST_PW})
    assert res.status_code == 200, res.text
    return auth_header(res.json())


def _res_id(res) -> str:
    body = res.json()
    rid = body.get("data", {}).get("id") or body.get("id")
    assert rid, res.text
    return rid


async def _comment_count(client: AsyncClient, post_id: str, headers: dict) -> int:
    res = await client.get(f"/v1/posts/{post_id}", headers=headers)
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    return data.get("commentCount", data.get("comment_count"))


async def _visible_comments(client: AsyncClient, post_id: str, headers: dict) -> int:
    res = await client.get(f"/v1/posts/{post_id}/comments?size=50", headers=headers)
    assert res.status_code == 200, res.text
    return len(res.json()["data"]["items"])


async def _post_with_comments(client: AsyncClient, headers: dict, n: int) -> tuple[str, list[str]]:
    res = await client.post(
        "/v1/posts",
        json={"title": "블라인드 카운트 테스트", "content": "본문"},
        headers={**headers, "X-Idempotency-Key": new_ulid_str()},
    )
    assert res.status_code == 201, res.text
    post_id = _res_id(res)
    comment_ids = []
    for i in range(n):
        c = await client.post(
            f"/v1/posts/{post_id}/comments", json={"content": f"댓글{i}"}, headers=headers
        )
        assert c.status_code == 201, c.text
        comment_ids.append(_res_id(c))
    return post_id, comment_ids


async def test_blind_comment_keeps_count_in_sync(client: AsyncClient, db_session: AsyncSession):
    """블라인드는 comment_count를 줄이고, 해제는 되돌린다 — 표시 개수와 어긋나지 않는다."""
    admin = await _admin_headers(client, db_session, "blind-admin@example.com", "블라인드관리자")
    author = await _author_headers(client, "blind-author@example.com", "블라인드작성자")

    post_id, comment_ids = await _post_with_comments(client, author, 3)
    assert await _comment_count(client, post_id, author) == 3
    assert await _visible_comments(client, post_id, author) == 3

    res = await client.patch(f"/v1/admin/comments/{comment_ids[0]}/blind", headers=admin)
    assert res.status_code == 200, res.text
    assert await _comment_count(client, post_id, author) == 2
    assert await _visible_comments(client, post_id, author) == 2  # 표시와 카운트가 일치

    res = await client.patch(f"/v1/admin/comments/{comment_ids[0]}/unblind", headers=admin)
    assert res.status_code == 200, res.text
    assert await _comment_count(client, post_id, author) == 3
    assert await _visible_comments(client, post_id, author) == 3


async def test_repeated_moderation_is_idempotent(client: AsyncClient, db_session: AsyncSession):
    """같은 동작을 반복해도 카운트가 어긋나면 안 된다 — 조정은 실제 상태 전이일 때만."""
    admin = await _admin_headers(client, db_session, "idem-admin@example.com", "멱등관리자")
    author = await _author_headers(client, "idem-author@example.com", "멱등작성자")

    post_id, comment_ids = await _post_with_comments(client, author, 3)
    target = comment_ids[0]

    for _ in range(3):  # 블라인드 반복 — 첫 회만 차감되어야 한다
        assert (
            await client.patch(f"/v1/admin/comments/{target}/blind", headers=admin)
        ).status_code == 200
    assert await _comment_count(client, post_id, author) == 2

    for _ in range(3):  # 해제 반복 — 첫 회만 복구되어야 한다
        assert (
            await client.patch(f"/v1/admin/comments/{target}/unblind", headers=admin)
        ).status_code == 200
    assert await _comment_count(client, post_id, author) == 3

    # reset-reports는 블라인드 해제를 겸한다 — 반복해도 카운트 불변.
    await client.patch(f"/v1/admin/comments/{target}/blind", headers=admin)
    assert await _comment_count(client, post_id, author) == 2
    for _ in range(3):
        assert (
            await client.patch(f"/v1/admin/comments/{target}/reset-reports", headers=admin)
        ).status_code == 200
    assert await _comment_count(client, post_id, author) == 3


async def test_deleting_blinded_comment_does_not_double_decrement(
    client: AsyncClient, db_session: AsyncSession
):
    """블라인드된 댓글을 삭제해도 한 댓글로 두 번 차감되지 않는다."""
    admin = await _admin_headers(client, db_session, "dbl-admin@example.com", "이중차감관리자")
    author = await _author_headers(client, "dbl-author@example.com", "이중차감작성자")

    post_id, comment_ids = await _post_with_comments(client, author, 3)
    target = comment_ids[0]

    await client.patch(f"/v1/admin/comments/{target}/blind", headers=admin)
    assert await _comment_count(client, post_id, author) == 2

    res = await client.delete(f"/v1/posts/{post_id}/comments/{target}", headers=author)
    assert res.status_code in (200, 204), res.text
    assert await _comment_count(client, post_id, author) == 2  # 3이 아니라 2에서 그대로
    assert await _visible_comments(client, post_id, author) == 2


async def test_blinding_root_accounts_for_its_replies(
    client: AsyncClient, db_session: AsyncSession
):
    """루트를 블라인드하면 대댓글까지 목록에서 사라지므로 카운트도 그만큼 줄어야 한다."""
    admin = await _admin_headers(client, db_session, "sub-admin@example.com", "서브트리관리자")
    author = await _author_headers(client, "sub-author@example.com", "서브트리작성자")

    post_id, comment_ids = await _post_with_comments(client, author, 1)
    root = comment_ids[0]
    for i in range(4):
        r = await client.post(
            f"/v1/posts/{post_id}/comments",
            json={"content": f"답글{i}", "parentId": root},
            headers=author,
        )
        assert r.status_code == 201, r.text
    assert await _comment_count(client, post_id, author) == 5

    res = await client.patch(f"/v1/admin/comments/{root}/blind", headers=admin)
    assert res.status_code == 200, res.text

    # 루트 1 + 대댓글 4가 통째로 사라진다 — 1만 깎으면 4가 어긋난다.
    assert await _visible_comments(client, post_id, author) == 0
    assert await _comment_count(client, post_id, author) == 0

    # 블라인드된 루트의 대댓글은 "더보기"로도 못 본다(목록과 일관).
    more = await client.get(f"/v1/posts/{post_id}/comments/{root}/replies", headers=author)
    assert more.status_code == 404, more.text

    # 해제하면 서브트리가 통째로 돌아온다.
    assert (
        await client.patch(f"/v1/admin/comments/{root}/unblind", headers=admin)
    ).status_code == 200
    assert await _comment_count(client, post_id, author) == 5
