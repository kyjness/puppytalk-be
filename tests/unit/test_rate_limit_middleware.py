"""RateLimit 미들웨어 배치·redis 탐색 단위 테스트 (실 Redis 없이 FakeRedis).

검증 불변식:
- redis 탐색은 scope["app"] 기반으로 동작한다(체인 .app 순회는 항상 None이 나오는 결함이었다).
- 429는 CORS·RED 메트릭·X-Request-ID를 "거쳐" 나간다 — RateLimit이 최안쪽이어야 성립.
"""

import pytest
from app.core.config import settings
from app.core.middleware.metrics import REQUESTS_TOTAL
from app.main import app
from starlette.testclient import TestClient

from tests.unit.fakes import FakeRedis as SharedFakeRedis

_UNMATCHED = "__unmatched__"


class FakeRedis(SharedFakeRedis):
    """rate limit Lua(INCR+EXPIRE+TTL) 의미론만 교체한 가짜."""

    def __init__(self) -> None:
        super().__init__()
        self.eval_calls = 0
        self.counts: dict[str, int] = {}

    async def eval(self, script, numkeys, key, window):  # pyright: ignore[reportIncompatibleMethodOverride]
        self.eval_calls += 1
        c = self.counts.get(key, 0) + 1
        self.counts[key] = c
        return [c, int(window)]


@pytest.fixture()
def client(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(app.state, "redis", fake, raising=False)
    with_client = TestClient(app)  # lifespan 미실행(DB/Redis 실연결 없음)
    yield with_client, fake
    # monkeypatch가 app.state.redis를 원복한다.


def _429_counter_value() -> float:
    return REQUESTS_TOTAL.labels(method="GET", path=_UNMATCHED, status=429)._value.get()


def test_redis_discovered_via_scope_and_429_passes_observability(client, monkeypatch):
    tc, fake = client
    monkeypatch.setattr(settings, "RATE_LIMIT_MAX_REQUESTS", 1)
    origin = settings.CORS_ORIGINS[0]
    before = _429_counter_value()

    ok = tc.get("/v1/", headers={"Origin": origin})
    assert ok.status_code == 200

    limited = tc.get("/v1/", headers={"Origin": origin})
    assert limited.status_code == 429

    # scope["app"] 기반 탐색이 동작해 Redis 경로(fixed window Lua)를 탔다.
    assert fake.eval_calls == 2

    # CORS 안쪽에서 429가 생성되어 브라우저가 응답을 읽을 수 있다.
    assert limited.headers.get("access-control-allow-origin") == origin
    assert "retry-after" in limited.headers

    # RequestId(최외곽)가 429에도 적용된다 — 헤더·바디 상관.
    assert limited.headers.get("x-request-id")
    assert limited.json()["requestId"] == limited.headers["x-request-id"]

    # RED 메트릭에 429가 집계된다(라우트 매칭 전이라 path=__unmatched__).
    assert _429_counter_value() == before + 1


def test_options_and_probe_paths_skip_rate_limit(client, monkeypatch):
    tc, fake = client
    monkeypatch.setattr(settings, "RATE_LIMIT_MAX_REQUESTS", 1)

    # probe 경로는 카운트 자체가 없다.
    assert tc.get("/livez").status_code == 200
    assert fake.eval_calls == 0


def test_signup_upload_limit_covers_presign_and_confirm(client, monkeypatch):
    """비인증 회원가입 업로드 한도는 presign·confirm 2단을 하나의 카운터로 조인다 —
    글로벌 100/분만 적용되면 비로그인 IP가 presign을 대량 발급받아 pending/ 객체를 쌓는다."""
    from app.core.middleware.rate_limit import _path_is_signup_upload, _resolve_policy

    tc, fake = client
    monkeypatch.setattr(settings, "SIGNUP_UPLOAD_RATE_LIMIT_MAX", 1)

    ok = tc.post("/v1/media/images/signup/presign", json={})
    assert ok.status_code != 429
    limited = tc.post("/v1/media/images/signup/confirm", json={})
    assert limited.status_code == 429, "presign과 confirm이 같은 signup_upload 카운터여야 한다"

    signup_keys = [k for k in fake.counts if k.startswith("rl:signup_upload:")]
    assert len(signup_keys) == 1 and fake.counts[signup_keys[0]] == 2

    # 남용 방어 경로 — Redis 장애 시 fail-open이 아니라 메모리 폴백을 타야 한다.
    for p in ("/v1/media/images/signup/presign", "/v1/media/images/signup/confirm"):
        policy = _resolve_policy(p)
        assert policy.key_prefix == "signup_upload" and not policy.fail_open
    # 직접 multipart 경로는 제거됐다 — 옛 경로가 전용 한도에 잡히면 안 된다(글로벌만).
    assert not _path_is_signup_upload("/v1/media/images/signup")
    assert _resolve_policy("/v1/media/images/signup").key_prefix == "global"
