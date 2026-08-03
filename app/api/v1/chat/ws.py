# 1:1 DM WebSocket. ?token= Access JWT, 메시지는 ChatService·Redis 팬아웃.

import json
import logging
import time

from fastapi import APIRouter, WebSocket
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from app.common.exceptions import (
    BaseProjectException,
    ForbiddenException,
    UnauthorizedException,
    UserNotFoundException,
)
from app.core.config import settings
from app.core.rate_limit import check_fixed_window, count_rejection
from app.db import AsyncSessionLocal
from app.domain.chat.manager import chat_connection_manager
from app.domain.chat.payload import parse_incoming_message, validation_error_to_ws_error
from app.domain.chat.schema import ChatWsErrorPayload
from app.domain.chat.service import DM_SAME_USER, ChatService
from app.domain.chat.ws_auth import authenticate_chat_websocket
from app.infra.redis import RedisLike, get_app_redis

log = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

# 한도 초과 후에도 계속 밀어붙이는 클라이언트는 끊는다(1008) — 한도 초과 스팸이
# 프레임당 응답 생성·Redis 왕복으로 남는 것조차 막는 마지막 단계.
_REJECT_CLOSE_THRESHOLD = 30


class _RejectionGate:
    """연결 단위 거부 상태. 거부되면 retry_after 동안 Redis 왕복 없이 로컬에서 즉시
    거부하고(스팸의 공유 Redis 부하 증폭 방지), 억제 창을 피해서 페이싱하든 말든
    **연속 거부 누계**가 임계에 닿으면 종료를 지시한다."""

    def __init__(self) -> None:
        self.blocked_until = 0.0
        self.consecutive_rejections = 0

    def suppressed(self, now: float) -> bool:
        return now < self.blocked_until

    def register_rejection(self, now: float, retry_after: int) -> bool:
        """거부 1건 기록. True면 연결을 끊어야 한다."""
        self.blocked_until = max(self.blocked_until, now + max(retry_after, 1))
        self.consecutive_rejections += 1
        return self.consecutive_rejections >= _REJECT_CLOSE_THRESHOLD

    def register_allowed(self) -> None:
        self.consecutive_rejections = 0


def _redis_from_websocket(websocket: WebSocket) -> RedisLike | None:
    return get_app_redis(websocket.scope.get("app"))


async def _safe_send_text(websocket: WebSocket, text: str) -> None:
    """끊긴/끊기는 중인 소켓에 에러 프레임을 보내다 나는 예외(RuntimeError 등)는
    WebSocketDisconnect가 아니라서 루프 밖으로 새면 ASGI 예외 소음이 된다 — 삼킨다."""
    try:
        await websocket.send_text(text)
    except Exception as e:
        log.debug("chat ws error-frame send skip: %s", e)


async def _send_ws_error(websocket: WebSocket, code: str, message: str) -> None:
    err = ChatWsErrorPayload(code=code, message=message).model_dump(mode="json", by_alias=True)
    await _safe_send_text(websocket, json.dumps(err, ensure_ascii=False))


@router.websocket("/ws/chat")
async def chat_dm_websocket(websocket: WebSocket) -> None:
    async with AsyncSessionLocal() as db:
        try:
            user_id = await authenticate_chat_websocket(websocket, db)
        except UnauthorizedException:
            await websocket.close(code=1008, reason="Unauthorized")
            return
        except ForbiddenException:
            await websocket.close(code=1008, reason="Forbidden")
            return
        except Exception:
            log.exception("chat ws auth 예외")
            await websocket.close(code=1011, reason="Internal error")
            return

    await websocket.accept()
    await chat_connection_manager.connect(user_id, websocket)
    redis = _redis_from_websocket(websocket)
    gate = _RejectionGate()
    try:
        while True:
            raw = await websocket.receive_text()
            # WS는 HTTP rate limit 미들웨어 밖 — 접속 1회로 무제한 DB 쓰기+팬아웃이
            # 가능하므로 유저 단위 한도를 수신 루프에서 직접 검사한다. parse보다 먼저:
            # 잘못된 프레임 스팸(검증 비용+에러 응답 루프)도 같은 한도에 잡혀야 한다.
            now = time.monotonic()
            if gate.suppressed(now):
                # 억제 창 안: RedisLike 왕복 없이 거부하되, 계측은 동일하게 남긴다.
                count_rejection("chat")
                if gate.register_rejection(now, int(gate.blocked_until - now)):
                    await websocket.close(code=1008, reason="Rate limit exceeded")
                    return
                await _send_ws_error(
                    websocket,
                    "rate_limited",
                    f"메시지 전송이 너무 잦습니다. {int(gate.blocked_until - now) + 1}초 후 다시 시도하세요.",
                )
                continue
            allowed, retry_after = await check_fixed_window(
                redis,
                f"chat:ws:{user_id}",
                window_sec=settings.CHAT_WS_RATE_LIMIT_WINDOW,
                max_count=settings.CHAT_WS_RATE_LIMIT_MAX_MESSAGES,
            )
            if not allowed:
                # 억제 창을 피해 페이싱해도 연속 거부 누계로 같은 임계에 닿는다.
                if gate.register_rejection(now, retry_after):
                    await websocket.close(code=1008, reason="Rate limit exceeded")
                    return
                await _send_ws_error(
                    websocket,
                    "rate_limited",
                    f"메시지 전송이 너무 잦습니다. {retry_after}초 후 다시 시도하세요.",
                )
                continue
            gate.register_allowed()
            try:
                parsed = parse_incoming_message(raw)
            except ValidationError as e:
                payload = validation_error_to_ws_error(e)
                await _safe_send_text(websocket, json.dumps(payload, ensure_ascii=False))
                continue
            try:
                async with AsyncSessionLocal() as db:
                    await ChatService.send_dm_from_ws(
                        db,
                        sender_id=user_id,
                        payload=parsed,
                        redis=redis,
                    )
            except ValueError as e:
                if e.args and e.args[0] == DM_SAME_USER:
                    await _send_ws_error(
                        websocket, "dm_same_user", "자기 자신에게는 메시지를 보낼 수 없습니다."
                    )
                    continue
                log.exception("chat send ValueError")
                await _send_ws_error(websocket, "bad_request", "요청을 처리할 수 없습니다.")
            except UserNotFoundException as e:
                await _send_ws_error(
                    websocket, "peer_not_found", e.message or "상대방을 찾을 수 없습니다."
                )
            except BaseProjectException as e:
                # HTTP 표면과 동일한 5xx 정책: 서버 로그 + 내부 상세 비노출.
                if e.status_code >= 500:
                    log.exception("chat ws domain 5xx user=%s", user_id)
                    await _send_ws_error(websocket, "internal_error", "일시적 오류입니다.")
                else:
                    await _send_ws_error(websocket, str(e.code), e.message or "")
            except Exception:
                log.exception("chat ws send 실패 user=%s", user_id)
                await _send_ws_error(websocket, "internal_error", "일시적 오류입니다.")
    except WebSocketDisconnect:
        pass
    finally:
        await chat_connection_manager.disconnect(user_id, websocket)
