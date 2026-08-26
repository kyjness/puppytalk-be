# TrustedHostMiddleware 래퍼 — 인프라 프로브 경로만 Host 검사에서 제외한다.

from typing import Any

from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import Receive, Scope, Send

from app.common.paths import INFRA_PROBE_PATHS


class ProbeAwareTrustedHostMiddleware(TrustedHostMiddleware):
    """`/livez`·`/readyz`·`/metrics`는 Host 검사를 건너뛴다.

    이 경로들을 부르는 것은 사용자가 아니라 **인프라**다 — Docker `HEALTHCHECK`는 컨테이너
    안에서 `localhost`로, ALB·kubelet은 파드 IP로 때린다. 그 값들은 `TRUSTED_HOSTS`
    (공개 도메인 목록)에 들어갈 이유가 없고, 넣으면 Host 검사 범위만 넓어진다.

    제외하지 않으면 **앱이 완전히 정상인데도 프로브가 400을 받아 영구 unhealthy**가 된다.
    실제로 첫 데모 배포에서 그렇게 됐다 — `Host: api.puppytalk.shop`은 200,
    `Host: localhost`는 400이었다.

    Host 검사를 건너뛰어도 노출되는 것은 프로브 3종뿐이다. 셋 다 요청 본문·인증을 받지 않고
    (`/readyz`는 DB·Redis 상태만, `/metrics`는 집계 수치만), rate limit·관측 미들웨어도
    같은 상수(`INFRA_PROBE_PATHS`)로 이미 이 경로들을 특별 취급한다 — 세 번째 합류다.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> Any:
        if scope["type"] == "http" and scope.get("path") in INFRA_PROBE_PATHS:
            return await self.app(scope, receive, send)
        return await super().__call__(scope, receive, send)
