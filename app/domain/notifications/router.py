# 알림 REST + SSE. 목록/읽음은 ApiResponse, 스트림은 text/event-stream.
# 오프라인 배송(SNS)은 알림 생성 시 서비스가 Celery로 오프로드한다 — 별도 재전달 API 없음.

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    CurrentUser,
    get_current_user,
    get_master_db,
    get_slave_db,
)
from app.common import (
    ApiCode,
    ApiResponse,
    CursorPage,
    OptionalPublicId,
    api_response,
)
from app.domain.notifications.schema import (
    MarkNotificationsReadData,
    MarkNotificationsReadRequest,
    NotificationItem,
)
from app.domain.notifications.service import NotificationService

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get(
    "/stream",
    status_code=200,
    summary="실시간 알림(SSE)",
    response_class=StreamingResponse,
)
async def notifications_stream(
    user: CurrentUser = Depends(get_current_user),
):
    """로컬 팬아웃 큐 기반 SSE. Redis 장애 시에도 스트림은 유지되고 같은 인스턴스
    이벤트는 계속 수신된다(fail-open) — 503으로 끊는 것보다 낫다."""

    # 등록을 먼저 — 상한 초과는 429로 끊는다(스트림이 시작되면 상태 코드를 못 바꾼다).
    queue = await NotificationService.sse_register(user.id)
    return StreamingResponse(
        NotificationService.sse_subscribe(user.id, queue),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("", status_code=200, response_model=ApiResponse[CursorPage[NotificationItem]])
async def list_notifications(
    request: Request,
    cursor: Annotated[
        OptionalPublicId,
        Query(description="무한 스크롤: 직전 응답의 마지막 알림 id(공개 ID). 미지정 시 처음부터."),
    ] = None,
    size: int = Query(20, ge=1, le=100),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_slave_db),
):
    items, has_more = await NotificationService.list_notifications(
        user.id, cursor_id=cursor, size=size, db=db
    )
    return api_response(request, code=ApiCode.OK, data=CursorPage(items=items, has_more=has_more))


@router.patch("/read", status_code=200, response_model=ApiResponse[MarkNotificationsReadData])
async def mark_notifications_read(
    request: Request,
    body: MarkNotificationsReadRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_master_db),
):
    n = await NotificationService.mark_read(user.id, ids=body.ids, db=db)
    return api_response(
        request,
        code=ApiCode.OK,
        data=MarkNotificationsReadData(updated_count=n),
    )
