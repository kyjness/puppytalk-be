"""인프라 프로브는 Host 검사를 건너뛴다 (첫 데모 배포에서 드러난 결함).

Docker `HEALTHCHECK`는 컨테이너 안에서 `localhost`로 `/livez`를 때린다. 그런데
`TRUSTED_HOSTS`는 공개 도메인 목록(`api.puppytalk.shop`)이라 `localhost`가 없다 —
앱이 완전히 정상인데도 프로브가 400을 받아 컨테이너가 **영구 unhealthy**가 됐다.

실측: `Host: api.puppytalk.shop` → 200, `Host: localhost` → 400.
"""

import pytest
from app.common.paths import INFRA_PROBE_PATHS
from app.core.middleware.trusted_host import ProbeAwareTrustedHostMiddleware
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

_ALLOWED = "api.puppytalk.shop"


def _client() -> TestClient:
    async def ok(request):
        return PlainTextResponse("ok")

    routes = [Route(p, ok) for p in sorted(INFRA_PROBE_PATHS)] + [Route("/v1/posts", ok)]
    app = Starlette(routes=routes)
    app.add_middleware(ProbeAwareTrustedHostMiddleware, allowed_hosts=[_ALLOWED])
    return TestClient(app)


@pytest.mark.parametrize("path", sorted(INFRA_PROBE_PATHS))
@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "10.0.1.23"])
def test_probe_paths_bypass_host_check(path, host):
    """컨테이너·ALB·kubelet이 쓰는 Host는 공개 도메인 목록에 없다 — 그래도 통과해야 한다."""
    assert _client().get(path, headers={"Host": host}).status_code == 200


def test_normal_path_still_rejects_untrusted_host():
    """프로브만 예외다 — 일반 경로의 Host 검사는 그대로 살아 있어야 한다."""
    assert _client().get("/v1/posts", headers={"Host": "evil.example.com"}).status_code == 400


def test_normal_path_allows_trusted_host():
    assert _client().get("/v1/posts", headers={"Host": _ALLOWED}).status_code == 200
