# 요청 기반 클라이언트 식별자.

from fastapi import Request

from app.core.middleware.proxy_headers import client_ip_from_scope


def get_client_identifier(request: Request) -> str:
    """조회수 dedup(viewer_key)용 클라이언트 식별자 — rate limit 키 산정과 동일하게
    프록시 검증이 끝난 scope["client"]만 쓴다(client_ip_from_scope가 규약을 소유)."""
    return client_ip_from_scope(request.scope, default="0.0.0.0").strip()
