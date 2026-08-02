# 4xx/5xx·슬로우 요청 접근 로그. request_id는 RequestIdFilter가 주입, 나머지는 extra로 구조화.
# 호출은 observability_middleware(단일 @app.middleware)가 담당한다.
import logging

from starlette.requests import Request

from app.core.config import settings
from app.core.middleware.proxy_headers import client_ip_from_scope

_access_logger = logging.getLogger("app.access")


def _fields(request: Request, status: int, duration_ms: float) -> dict[str, object]:
    return {
        "method": request.method,
        "path": request.url.path,
        "status": status,
        "duration_ms": round(duration_ms, 2),
        "client_ip": client_ip_from_scope(request.scope),
    }


def log_access(request: Request, status: int, duration_ms: float) -> None:
    """4xx/5xx·슬로우만 기록 — 대부분의 요청(빠른 2xx)은 필드 조립조차 하지 않는다."""
    is_slow = duration_ms >= settings.SLOW_REQUEST_MS
    if status < 400 and not is_slow:
        return
    fields = _fields(request, status, duration_ms)
    if status >= 500:
        _access_logger.error("access", extra=fields)
    elif status >= 400:
        _access_logger.warning("access", extra=fields)
    if is_slow:
        _access_logger.warning("slow request", extra=fields)


def log_unhandled_exception(request: Request, duration_ms: float) -> None:
    _access_logger.exception("unhandled exception", extra=_fields(request, 500, duration_ms))
