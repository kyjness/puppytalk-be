# 응답 후처리 관측 미들웨어 — 보안 헤더 + 접근 로그 + RED 메트릭을 한 겹에서 처리한다.
# 이전에는 세 개의 @app.middleware("http")(=BaseHTTPMiddleware) 층이었는데, 층마다
# 요청당 task·스트림 쌍을 만들고 perf_counter 타이머도 중복이었다 — 한 겹으로 합쳐
# 할당을 1/3로 줄이고 타이머를 공유한다. 적용 순서는 기존 스택(안쪽→바깥쪽:
# 보안 헤더 → 접근 로그 → 메트릭)을 그대로 재현한다.
import time
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

from app.common.paths import INFRA_PROBE_PATHS
from app.core.config import settings
from app.core.middleware.access_log import log_access, log_unhandled_exception
from app.core.middleware.metrics import (
    IN_PROGRESS,
    REQUEST_DURATION,
    REQUESTS_TOTAL,
    route_template_label,
)
from app.core.middleware.security_headers import apply_security_headers


async def observability_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    path = request.url.path
    # 메트릭만 프로브·/metrics 자신을 제외한다(카디널리티·자기 계측 방지).
    # 보안 헤더·접근 로그는 기존과 동일하게 프로브에도 적용된다.
    record_metrics = path not in INFRA_PROBE_PATHS
    method = request.method

    start = time.perf_counter()
    pending = IN_PROGRESS.labels(method=method) if record_metrics else None
    if pending is not None:
        pending.inc()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception:
        log_unhandled_exception(request, (time.perf_counter() - start) * 1000)
        raise
    finally:
        if pending is not None:
            duration = time.perf_counter() - start
            pending.dec()
            route_path = route_template_label(request)
            REQUESTS_TOTAL.labels(method=method, path=route_path, status=status).inc()
            REQUEST_DURATION.labels(method=method, path=route_path).observe(duration)

    duration_ms = (time.perf_counter() - start) * 1000
    apply_security_headers(response, path)
    if settings.DEBUG:
        response.headers["X-Process-Time"] = f"{duration_ms:.2f}"
    log_access(request, status, duration_ms)
    return response
