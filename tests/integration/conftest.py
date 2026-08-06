import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.api.dependencies import get_master_db, get_slave_db
from app.core.config import settings
from app.db.base import Base
from app.main import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# 기본값은 docker-compose.yml·.env.example의 로컬 스택과 같아야 한다 — 여기만 다른 값을
# 들고 있으면 TEST_DB_URL을 매번 export해야 통합 테스트가 돈다(CI는 잡 env로 넘겨서
# 티가 안 나고, 로컬에서만 인증 실패로 터진다).
TEST_DB_URL = os.getenv(
    "TEST_DB_URL",
    "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/puppytalk_test",
)


@pytest.fixture(scope="session", autouse=True)
def relax_integration_rate_limits() -> None:
    """통합 스위트는 단일 ASGI client IP + Redis 부재(메모리 폴백)라, rate limit 카운터가
    세션 내내 IP 키 하나로 누적된다. 스위트가 커지면 login뿐 아니라 global(기본 100/창)·
    signup_upload 한도까지 넘겨 순서 의존적 429가 나므로, 세 한도를 모두 넉넉히 완화한다."""
    settings.LOGIN_RATE_LIMIT_MAX_ATTEMPTS = max(settings.LOGIN_RATE_LIMIT_MAX_ATTEMPTS, 10_000)
    settings.RATE_LIMIT_MAX_REQUESTS = max(settings.RATE_LIMIT_MAX_REQUESTS, 1_000_000)
    settings.SIGNUP_UPLOAD_RATE_LIMIT_MAX = max(settings.SIGNUP_UPLOAD_RATE_LIMIT_MAX, 10_000)


try:
    make_url(TEST_DB_URL)
except Exception as e:
    raise RuntimeError(
        f"유효하지 않은 TEST_DB_URL입니다: {TEST_DB_URL!r}. 환경 변수를 확인하세요."
    ) from e

test_engine = create_async_engine(TEST_DB_URL, echo=False)
TestSessionLocal = async_sessionmaker(
    test_engine, class_=AsyncSession, expire_on_commit=False, autobegin=True
)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def setup_test_db(relax_integration_rate_limits):
    async with test_engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.create_all)

    yield

    async with test_engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    async with TestSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    # get_current_user 등은 get_slave_db(Reader)를 쓰므로 Writer만 오버라이드하면 401이 난다.
    async def override_test_db():
        yield db_session

    app.dependency_overrides[get_master_db] = override_test_db
    app.dependency_overrides[get_slave_db] = override_test_db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.pop(get_master_db, None)
    app.dependency_overrides.pop(get_slave_db, None)


# --- 인증 공용 헬퍼 ---
# accessToken/access_token 폴백은 응답 봉투의 세부사항이다 — 파일마다 복사하면 봉투가
# 바뀔 때 손댈 곳이 그만큼 늘어난다. conftest는 같은 디렉터리 전체가 자동으로 본다.


def auth_header(login_json: dict) -> dict[str, str]:
    """로그인 응답 → Authorization 헤더."""
    data = login_json.get("data", login_json)
    token = data.get("accessToken") or data.get("access_token")
    assert token, f"accessToken 없음: {login_json}"
    return {"Authorization": f"Bearer {token}"}


async def signup_login(
    client: AsyncClient, email: str, nickname: str, *, password: str
) -> dict[str, str]:
    """가입 후 로그인해 Authorization 헤더를 돌려준다(이미 가입돼 있어도 로그인만 한다)."""
    await client.post(
        "/v1/auth/signup", json={"email": email, "password": password, "nickname": nickname}
    )
    res = await client.post("/v1/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.text
    return auth_header(res.json())
