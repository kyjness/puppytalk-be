# Request ID 발급·scope.state 주입·X-Request-ID 응답 헤더. 순수 ASGI.
# main에서 add_middleware로 가장 마지막(코드상 최하단) 등록 → LIFO로 요청 진입 시 가장 먼저 실행.
import contextvars
from collections.abc import MutableMapping
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.ids import new_ulid_str

request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")


class RequestIdMiddleware:
    """모든 HTTP 요청에 ULID 기반 request_id 발급, scope['state']['request_id'] 저장, 응답 헤더에 X-Request-ID 주입."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        request_id = new_ulid_str()
        state["request_id"] = request_id

        token = request_id_ctx.set(request_id)
        try:

            async def send_wrapper(message: MutableMapping[str, Any]) -> None:
                if message.get("type") == "http.response.start":
                    # ASGI 메시지는 가변 dict — 리스트·dict 사본 없이 제자리에서 추가한다.
                    message.setdefault("headers", []).append(
                        (b"x-request-id", request_id.encode("utf-8"))
                    )
                await send(message)

            await self.app(scope, receive, send_wrapper)
        finally:
            request_id_ctx.reset(token)
