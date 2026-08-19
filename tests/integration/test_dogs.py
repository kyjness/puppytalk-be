import pytest
from app.core.ids import new_ulid_str
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.asyncio


def _token_from_login(res_json: dict) -> str:
    token_data = res_json.get("data", res_json)
    t = token_data.get("accessToken") or token_data.get("access_token")
    if not t:
        raise AssertionError("로그인 응답에 accessToken이 없습니다.")
    return t


async def setup_auth_user(client: AsyncClient, email: str, nickname: str) -> dict[str, str]:
    payload = {"email": email, "password": "TestPassword123!", "nickname": nickname}
    await client.post("/v1/auth/signup", json=payload)
    login_res = await client.post(
        "/v1/auth/login",
        json={"email": email, "password": payload["password"]},
    )
    assert login_res.status_code == 200, login_res.text
    return {"Authorization": f"Bearer {_token_from_login(login_res.json())}"}


def _dog(name: str, *, representative: bool = False) -> dict:
    return {
        "name": name,
        "breed": "말티즈",
        "gender": "male",
        "birthDate": "2021-03-01",
        "isRepresentative": representative,
    }


async def test_profile_returns_full_dogs_and_representative(client: AsyncClient):
    # 대표견 전용 뷰 관계로 옮긴 뒤에도 프로필은 전체 dogs 목록과 대표견을 함께 내려야 한다.
    # (옛 .and_() 필터 로드는 dogs 컬렉션을 대표견 1마리로 truncate하는 트랩이 있었다.)
    headers = await setup_auth_user(client, "dog_profile@example.com", "프로필퍼피")
    res = await client.patch(
        "/v1/users/me",
        json={"dogs": [_dog("초코", representative=True), _dog("바둑")]},
        headers=headers,
    )
    assert res.status_code == 200, res.text

    me = await client.get("/v1/users/me", headers=headers)
    assert me.status_code == 200, me.text
    data = me.json().get("data", me.json())
    # 전체 dogs가 truncate되지 않고 2마리 모두 보인다.
    assert len(data["dogs"]) == 2
    rep = data.get("representativeDog")
    assert rep is not None
    assert rep["name"] == "초코"
    # dogs 목록 안에서도 대표 플래그가 정확히 1개.
    flagged = [d for d in data["dogs"] if d.get("isRepresentative")]
    assert len(flagged) == 1
    assert flagged[0]["name"] == "초코"


async def test_set_representative_switches_single(client: AsyncClient):
    headers = await setup_auth_user(client, "dog_switch@example.com", "전환퍼피")
    await client.patch(
        "/v1/users/me",
        json={"dogs": [_dog("초코", representative=True), _dog("바둑")]},
        headers=headers,
    )
    me = await client.get("/v1/users/me", headers=headers)
    dogs = me.json().get("data", me.json())["dogs"]
    other = next(d for d in dogs if d["name"] == "바둑")

    res = await client.patch(
        "/v1/users/me/dogs/representative",
        json={"dogId": other["id"]},
        headers=headers,
    )
    assert res.status_code == 200, res.text
    data = res.json().get("data", res.json())
    assert data["representativeDog"]["name"] == "바둑"
    flagged = [d for d in data["dogs"] if d.get("isRepresentative")]
    assert len(flagged) == 1
    assert flagged[0]["name"] == "바둑"


async def test_post_author_carries_representative_dog(client: AsyncClient):
    headers = await setup_auth_user(client, "dog_author@example.com", "작성자퍼피")
    await client.patch(
        "/v1/users/me",
        json={"dogs": [_dog("초코", representative=True), _dog("바둑")]},
        headers=headers,
    )
    create = await client.post(
        "/v1/posts",
        json={"title": "우리 초코 자랑", "content": "내용"},
        headers={**headers, "X-Idempotency-Key": new_ulid_str()},
    )
    assert create.status_code == 201, create.text
    post_id = create.json().get("data", {}).get("id")

    detail = await client.get(f"/v1/posts/{post_id}", headers=headers)
    assert detail.status_code == 200
    author = detail.json().get("data", {}).get("author", {})
    assert author.get("representativeDog", {}).get("name") == "초코"


async def test_partial_unique_index_rejects_second_representative(db_session):
    # API는 set_representative로 대표견을 1개로 정규화하므로 2개를 만들 수 없다.
    # DB 부분 유니크 인덱스가 최후 방어선임을 직접 검증한다: 같은 owner에 대표견 2개 → IntegrityError.
    from app.db.base_class import utc_now
    from app.domain.dogs.model import DogProfile
    from app.domain.users.model import User

    now = utc_now()
    user = User(
        email="idx_owner@example.com",
        password="x",
        nickname="인덱스오너",
        status="ACTIVE",
        created_at=now,
        updated_at=now,
    )
    db_session.add(user)
    await db_session.flush()

    common = dict(
        owner_id=user.id,
        breed="말티즈",
        gender="male",
        birth_date="2021-03-01",
        created_at=now,
        updated_at=now,
    )
    db_session.add(DogProfile(name="초코", is_representative=True, **common))
    db_session.add(DogProfile(name="바둑", is_representative=True, **common))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_dog_profile_rejects_soft_deleted_image(client: AsyncClient, db_session):
    """수거 대기 중인 이미지는 개 프로필에 붙일 수 없다.

    붙게 두면 스위퍼의 참조 검사가 "아직 쓰인다"로 보아 그 행을 **영구히** 수거하지 못하고,
    지운 사진이 화면에 되살아난다. users·posts는 첨부 검증을 거치고 있었는데 dogs만
    payload의 id를 그대로 bulk upsert에 넘겨 뚫려 있었다(ADR 0019 결정 3).
    """
    import uuid

    from app.db.base_class import utc_now
    from app.domain.media.model import Image
    from sqlalchemy import delete

    now = utc_now()
    sfx = uuid.uuid4().hex[:8]

    def _image(tag: str, deleted_at) -> Image:
        key = f"dog-attach-{tag}-{sfx}"
        return Image(
            file_key=key,
            file_url=f"http://example.test/{key}",
            content_type="image/png",
            size=1,
            uploader_id=None,
            created_at=now,
            deleted_at=deleted_at,
        )

    live, dead = _image("live", None), _image("dead", now)
    db_session.add_all([live, dead])
    await db_session.commit()
    live_id, dead_id = str(live.id), str(dead.id)

    headers = await setup_auth_user(client, f"dog_image_{sfx}@example.com", f"퍼피{sfx[:6]}")

    try:
        ok = await client.patch(
            "/v1/users/me",
            json={"dogs": [{**_dog("초코"), "profileImageId": live_id}]},
            headers=headers,
        )
        assert ok.status_code == 200, ok.text

        rejected = await client.patch(
            "/v1/users/me",
            json={"dogs": [{**_dog("바둑"), "profileImageId": dead_id}]},
            headers=headers,
        )
        assert rejected.status_code == 400, (
            f"수거 대기 이미지가 개 프로필에 붙었다 — 영구 누수 경로다: {rejected.text}"
        )
    finally:
        # 남기면 스위퍼가 수거 대상으로 집어 다른 테스트의 기대값을 흔든다.
        await db_session.rollback()
        await db_session.execute(delete(Image).where(Image.file_key.like(f"dog-attach-%-{sfx}")))
        await db_session.commit()
