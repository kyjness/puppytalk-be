# HTTP 요청 rate limit 미들웨어 — 경로별 정책 테이블로 (키 접두사·한도·응답 코드·fail-open)을
# 한 곳에서 결정하고, 검사 자체는 core.rate_limit(check_fixed_window)에 위임한다.
# 순수 ASGI(scope/receive/send). main에서 add_middleware(RateLimitMiddleware)로 등록.
import json
from collections.abc import Callable
from typing import NamedTuple

from starlette.types import ASGIApp, Receive, Scope, Send

from app.common import ApiCode
from app.common.paths import (
    INFRA_PROBE_PATHS,
    LOGIN_PATH,
    SIGNUP_CONFIRM_PATH,
    SIGNUP_PRESIGN_PATH,
)
from app.common.responses import error_body, retry_after_fields
from app.core.config import settings
from app.core.middleware.proxy_headers import client_ip_from_scope
from app.core.rate_limit import check_fixed_window
from app.infra.redis import RedisLike, get_app_redis


def _redis_from_scope(scope: Scope) -> RedisLike | None:
    """Starlette가 매 요청 scope["app"]에 심는 앱 인스턴스에서 redis를 얻는다.

    미들웨어 체인 객체를 .app으로 거슬러 올라가는 방식은 어떤 노드도 .state를
    갖지 않아 항상 None이 나온다(분산 rate limit이 조용히 비활성화되는 결함).
    """
    return get_app_redis(scope.get("app"))


def _path_is_login(path: str) -> bool:
    return path.rstrip("/") == LOGIN_PATH


def _path_is_signup_upload(path: str) -> bool:
    """비인증 회원가입 업로드 경로(presign·confirm 2단). 글로벌 100/분만으로는 비로그인
    IP가 presign을 대량 발급받아 pending/ 객체를 쌓을 수 있어 전용 한도로 조인다."""
    p = path.rstrip("/")
    return p in (SIGNUP_PRESIGN_PATH, SIGNUP_CONFIRM_PATH)


class _RatePolicy(NamedTuple):
    """경로 한 갈래의 rate limit 정책. 한도는 테스트·런타임 재설정이 반영되도록
    settings를 요청 시점에 읽는 thunk로 둔다."""

    matches: Callable[[str], bool]
    key_prefix: str
    code: ApiCode
    # False = Redis 장애 시 메모리 폴백(남용 방어 경로). True = 통과(가용성 우선).
    fail_open: bool
    limits: Callable[[], tuple[int, int]]  # (window_sec, max_count)


# 매칭 우선순위 순. 글로벌은 항상 마지막 폴백 행 — 행 하나가 (매칭·키·코드·fail-open·한도)를
# 전부 소유하므로, 경로를 추가할 때 별도 술어(_is_critical_path류)와 어긋날 표면이 없다.
_POLICIES: tuple[_RatePolicy, ...] = (
    _RatePolicy(
        _path_is_login,
        "login",
        ApiCode.LOGIN_RATE_LIMIT_EXCEEDED,
        False,
        lambda: (settings.LOGIN_RATE_LIMIT_WINDOW, settings.LOGIN_RATE_LIMIT_MAX_ATTEMPTS),
    ),
    _RatePolicy(
        _path_is_signup_upload,
        "signup_upload",
        ApiCode.RATE_LIMIT_EXCEEDED,
        False,
        lambda: (settings.SIGNUP_UPLOAD_RATE_LIMIT_WINDOW, settings.SIGNUP_UPLOAD_RATE_LIMIT_MAX),
    ),
    _RatePolicy(
        lambda _path: True,
        "global",
        ApiCode.RATE_LIMIT_EXCEEDED,
        True,
        lambda: (settings.RATE_LIMIT_WINDOW, settings.RATE_LIMIT_MAX_REQUESTS),
    ),
)


def _resolve_policy(path: str) -> _RatePolicy:
    return next(p for p in _POLICIES if p.matches(path))


async def _send_429(send: Send, scope: Scope, code: ApiCode, retry_after_seconds: int) -> None:
    """순수 ASGI: 429 응답만 전송. 바디 규격은 error_body가 단일 소스."""
    state = scope.get("state") or {}
    rid = state.get("request_id", "") or ""
    data, retry_after_value = retry_after_fields(retry_after_seconds)
    body = json.dumps(
        error_body(code.value, "", data, request_id=rid),
        ensure_ascii=False,
    ).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"retry-after", retry_after_value.encode()),
    ]
    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": body})


class RateLimitMiddleware:
    """순수 ASGI 미들웨어. BaseHTTPMiddleware 미사용. scope/receive/send만 사용.

    프로브·계측 경로만 한도 제외. /v1/health는 비인증 + 요청마다 DB 왕복이라 글로벌
    한도를 그대로 태운다 — 프로브는 /livez·/readyz가 전담하므로 한도 제외로 열어둘
    이유가 없다(무한도 DB ping 표면).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")
        if method == "OPTIONS" or path in INFRA_PROBE_PATHS:
            await self.app(scope, receive, send)
            return

        ip = client_ip_from_scope(scope)
        policy = _resolve_policy(path)
        window_sec, max_count = policy.limits()
        allowed, retry_after_seconds = await check_fixed_window(
            _redis_from_scope(scope),
            f"{policy.key_prefix}:{ip}",
            window_sec=window_sec,
            max_count=max_count,
            fail_open=policy.fail_open,
        )
        if not allowed:
            await _send_429(send, scope, policy.code, retry_after_seconds)
            return

        await self.app(scope, receive, send)
