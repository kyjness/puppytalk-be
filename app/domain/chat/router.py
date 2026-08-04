# 1:1 DM 라우터. REST(방·메시지 커서 페이지네이션) + WebSocket(?token= 인증, 수신 루프).

import json
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request, WebSocket
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocketDisconnect

from app.api.dependencies import (
    CurrentUser,
    get_current_user,
    get_master_db,
    get_slave_db,
    resolve_access_token_user,
)
from app.common import (
    ApiCode,
    ApiResponse,
    CursorPage,
    OptionalPublicId,
    PublicId,
    api_response,
)
from app.common.exceptions import (
    BaseProjectException,
    UnauthorizedException,
    UserNotFoundException,
    ws_close_code,
)
from app.core.config import settings
from app.core.rate_limit import LocalRejectionGate
from app.db import AsyncSessionLocal, AsyncSessionLocalReader
from app.domain.chat.manager import chat_connection_manager
from app.domain.chat.payload import parse_incoming_message, validation_error_detail
from app.domain.chat.schema import (
    ChatDirectRoomData,
    ChatMessageItem,
    ChatRoomPeerInfoData,
    ChatRoomsListData,
    ChatWsErrorPayload,
)
from app.domain.chat.service import ChatService
from app.infra.redis import get_websocket_redis

log = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])
ws_router = APIRouter(tags=["chat"])


@router.post(
    "/rooms/direct/{peer_user_id}",
    status_code=200,
    response_model=ApiResponse[ChatDirectRoomData],
)
async def get_or_create_direct_room(
    request: Request,
    peer_user_id: Annotated[PublicId, Path(..., description="상대방 공개 사용자 ID (Base62)")],
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_master_db),
):
    room_id = await ChatService.resolve_direct_room(db, user_id=user.id, peer_id=peer_user_id)
    return api_response(request, code=ApiCode.OK, data=ChatDirectRoomData(room_id=room_id))


@router.get(
    "/rooms",
    status_code=200,
    response_model=ApiResponse[ChatRoomsListData],
)
async def list_recent_rooms(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    limit: int = Query(20, ge=1, le=50, description="최근 대화 목록 개수"),
    db: AsyncSession = Depends(get_slave_db),
):
    data = await ChatService.list_recent_rooms(db, user_id=user.id, limit=limit)
    return api_response(request, code=ApiCode.OK, data=data)


@router.get(
    "/rooms/{room_id}",
    status_code=200,
    response_model=ApiResponse[ChatRoomPeerInfoData],
)
async def get_room_peer_info(
    request: Request,
    room_id: Annotated[PublicId, Path(..., description="채팅방 공개 ID (Base62)")],
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_slave_db),
):
    data = await ChatService.get_room_peer_info(db, room_id=room_id, user_id=user.id)
    return api_response(request, code=ApiCode.OK, data=data)


@router.post(
    "/rooms/{room_id}/read",
    status_code=200,
    response_model=ApiResponse[None],
)
async def mark_room_read(
    request: Request,
    room_id: Annotated[PublicId, Path(..., description="채팅방 공개 ID (Base62)")],
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_master_db),
):
    await ChatService.mark_room_read(db, room_id=room_id, user_id=user.id)
    return api_response(request, code=ApiCode.OK)


@router.get(
    "/rooms/{room_id}/messages",
    status_code=200,
    response_model=ApiResponse[CursorPage[ChatMessageItem]],
)
async def list_room_messages(
    request: Request,
    room_id: Annotated[PublicId, Path(..., description="채팅방 공개 ID (Base62)")],
    user: CurrentUser = Depends(get_current_user),
    cursor: Annotated[
        OptionalPublicId,
        Query(
            description="무한 스크롤: 직전 응답의 마지막 메시지 id(공개 ID). 미지정 시 최신부터."
        ),
    ] = None,
    limit: int = Query(30, ge=1, le=100, description="한 번에 가져올 최대 개수"),
    db: AsyncSession = Depends(get_slave_db),
):
    items, has_more = await ChatService.list_room_messages(
        db,
        room_id=room_id,
        user_id=user.id,
        cursor_message_id=cursor,
        limit=limit,
    )
    return api_response(request, code=ApiCode.OK, data=CursorPage(items=items, has_more=has_more))


# --- WebSocket ---

# 한도 초과 후에도 계속 밀어붙이는 클라이언트는 끊는다(1008) — 한도 초과 스팸이
# 프레임당 응답 생성·Redis 왕복으로 남는 것조차 막는 마지막 단계.
_REJECT_CLOSE_THRESHOLD = 30


def _ws_close_reason(exc: BaseProjectException) -> str:
    if exc.status_code >= 500:
        return "Internal error"
    return {401: "Unauthorized", 403: "Forbidden"}.get(exc.status_code, "Policy violation")


async def _authenticate(websocket: WebSocket, db: AsyncSession) -> UUID:
    """?token= Access JWT 인증 — HTTP Depends와 동일 파이프라인(resolve_access_token_user)."""
    token = (websocket.query_params.get("token") or "").strip()
    if not token:
        raise UnauthorizedException(message="인증 토큰이 필요합니다.")
    user = await resolve_access_token_user(
        token, db=db, redis_client=get_websocket_redis(websocket)
    )
    return user.id


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


@ws_router.websocket("/ws/chat")
async def chat_dm_websocket(websocket: WebSocket) -> None:
    # 인증은 순수 읽기 — writer 풀을 점유하면 재연결 폭풍이 쓰기 경로와 커넥션을 다툰다.
    async with AsyncSessionLocalReader() as db:
        try:
            user_id = await _authenticate(websocket, db)
        except BaseProjectException as e:
            if e.status_code >= 500:
                log.exception("chat ws auth 예외")
            await websocket.close(code=ws_close_code(e), reason=_ws_close_reason(e))
            return
        except Exception:
            log.exception("chat ws auth 예외")
            await websocket.close(code=1011, reason="Internal error")
            return

    await websocket.accept()
    await chat_connection_manager.connect(user_id, websocket)
    redis = get_websocket_redis(websocket)
    gate = LocalRejectionGate(f"chat:ws:{user_id}", close_threshold=_REJECT_CLOSE_THRESHOLD)
    try:
        while True:
            raw = await websocket.receive_text()
            # WS는 HTTP rate limit 미들웨어 밖 — 접속 1회로 무제한 DB 쓰기+팬아웃이
            # 가능하므로 유저 단위 한도를 수신 루프에서 직접 검사한다. parse보다 먼저:
            # 잘못된 프레임 스팸(검증 비용+에러 응답 루프)도 같은 한도에 잡혀야 한다.
            allowed, retry_after, should_close = await gate.check(
                redis,
                window_sec=settings.CHAT_WS_RATE_LIMIT_WINDOW,
                max_count=settings.CHAT_WS_RATE_LIMIT_MAX_MESSAGES,
            )
            if not allowed:
                if should_close:
                    await websocket.close(code=1008, reason="Rate limit exceeded")
                    return
                await _send_ws_error(
                    websocket,
                    "rate_limited",
                    f"메시지 전송이 너무 잦습니다. {retry_after}초 후 다시 시도하세요.",
                )
                continue
            try:
                parsed = parse_incoming_message(raw)
            except ValidationError as e:
                await _send_ws_error(websocket, "validation_error", validation_error_detail(e))
                continue
            try:
                async with AsyncSessionLocal() as send_db:
                    await ChatService.send_dm_from_ws(
                        send_db,
                        sender_id=user_id,
                        payload=parsed,
                        redis=redis,
                    )
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
