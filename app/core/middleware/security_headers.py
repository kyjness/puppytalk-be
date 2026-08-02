# 보안 헤더. X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy, CSP.
# 적용은 observability_middleware(단일 @app.middleware)가 응답 직후 호출한다.
from starlette.responses import Response

from app.core.config import settings

# 전부 정적 설정 파생 — 매 요청 f-string·경로 조립을 반복하지 않도록 import 시 1회 계산.
_HSTS_VALUE = f"max-age={settings.HSTS_MAX_AGE}; includeSubDomains"
_prefix = settings.API_PREFIX.rstrip("/")
# Swagger/ReDoc은 CDN·인라인 스크립트를 쓰므로 엄격한 CSP(self만 허용)와 충돌.
# 실무: docs 경로는 CSP 미적용(내부/개발용으로 간주). API_PREFIX 기준으로 판별.
# /docs/oauth2-redirect 등 하위 경로는 startswith가 포섭한다.
_DOCS_EXACT_PATHS = frozenset({f"{_prefix}/openapi.json", f"{_prefix}/redoc"})
_DOCS_PREFIX = f"{_prefix}/docs"


def apply_security_headers(response: Response, path: str) -> None:
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = settings.REFERRER_POLICY
    response.headers["Permissions-Policy"] = settings.PERMISSIONS_POLICY
    if settings.HSTS_ENABLED:
        response.headers["Strict-Transport-Security"] = _HSTS_VALUE
    is_docs_path = path in _DOCS_EXACT_PATHS or path.startswith(_DOCS_PREFIX)
    if settings.CONTENT_SECURITY_POLICY and not is_docs_path:
        response.headers["Content-Security-Policy"] = settings.CONTENT_SECURITY_POLICY
