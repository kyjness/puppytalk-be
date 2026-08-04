# 관리자 전용 API. 관리자 검증은 APIRouter.dependencies 로 일괄 적용.
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Path, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_admin, get_master_db
from app.common import ApiCode, ApiResponse, PaginatedResponse, PublicId, api_response
from app.common.enums import TargetType
from app.domain.admin.schema import (
    ActivatedResponse,
    BlindedResponse,
    MediaSweepResponse,
    ReportedPostItem,
    ResetReportsResponse,
    SuspendedResponse,
    UnblindedResponse,
)
from app.domain.admin.service import AdminService
from app.domain.media.service import MediaService
from app.infra.redis import get_app_redis

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(get_current_admin)],
)


@router.post(
    "/media/sweep",
    status_code=202,
    response_model=ApiResponse[MediaSweepResponse],
)
async def sweep_unused_media(
    request: Request,
    background_tasks: BackgroundTasks,
):
    # 세션·잡 락·로깅은 MediaService가 소유(202는 '시작'만 보장). redis를 넘겨
    # 스케줄 sweep과 같은 락을 공유한다.
    background_tasks.add_task(MediaService.sweep_unused_images_detached, get_app_redis(request.app))
    return api_response(
        request,
        code=ApiCode.OK,
        data=MediaSweepResponse(),
        message="백그라운드에서 정리가 시작되었습니다.",
    )


@router.get(
    "/reported-posts",
    status_code=200,
    response_model=ApiResponse[PaginatedResponse[ReportedPostItem]],
)
async def get_reported_posts(
    request: Request,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_master_db),
):
    items, total = await AdminService.get_reported_posts(page=page, size=size, db=db)
    has_more = (page * size) < total
    return api_response(
        request,
        code=ApiCode.OK,
        data=PaginatedResponse(items=items, has_more=has_more, total=total),
    )


@router.patch(
    "/posts/{post_id}/unblind",
    status_code=200,
    response_model=ApiResponse[UnblindedResponse],
)
async def unblind_post(
    request: Request,
    post_id: Annotated[PublicId, Path(..., description="게시글 공개 ID (Base62)")],
    db: AsyncSession = Depends(get_master_db),
):
    await AdminService.unblind(TargetType.POST, post_id, db=db)
    return api_response(request, code=ApiCode.OK, data=UnblindedResponse())


@router.patch(
    "/posts/{post_id}/reset-reports",
    status_code=200,
    response_model=ApiResponse[ResetReportsResponse],
)
async def reset_post_reports(
    request: Request,
    post_id: Annotated[PublicId, Path(..., description="게시글 공개 ID (Base62)")],
    db: AsyncSession = Depends(get_master_db),
):
    await AdminService.reset_reports(TargetType.POST, post_id, db=db)
    return api_response(request, code=ApiCode.OK, data=ResetReportsResponse())


@router.patch(
    "/users/{user_id}/suspend",
    status_code=200,
    response_model=ApiResponse[SuspendedResponse],
)
async def suspend_user(
    request: Request,
    user_id: Annotated[PublicId, Path(..., description="사용자 공개 ID (Base62)")],
    db: AsyncSession = Depends(get_master_db),
):
    redis = get_app_redis(request.app)
    await AdminService.suspend_user(user_id, db=db, redis=redis)
    return api_response(request, code=ApiCode.OK, data=SuspendedResponse())


@router.patch(
    "/users/{user_id}/activate",
    status_code=200,
    response_model=ApiResponse[ActivatedResponse],
)
async def activate_user(
    request: Request,
    user_id: Annotated[PublicId, Path(..., description="사용자 공개 ID (Base62)")],
    db: AsyncSession = Depends(get_master_db),
):
    redis = get_app_redis(request.app)
    await AdminService.activate_user(user_id, db=db, redis=redis)
    return api_response(request, code=ApiCode.OK, data=ActivatedResponse())


@router.patch(
    "/posts/{post_id}/blind",
    status_code=200,
    response_model=ApiResponse[BlindedResponse],
)
async def blind_post(
    request: Request,
    post_id: Annotated[PublicId, Path(..., description="게시글 공개 ID (Base62)")],
    db: AsyncSession = Depends(get_master_db),
):
    await AdminService.blind(TargetType.POST, post_id, db=db)
    return api_response(request, code=ApiCode.OK, data=BlindedResponse())


@router.delete(
    "/posts/{post_id}",
    status_code=200,
    response_model=ApiResponse[None],
)
async def delete_post_admin(
    request: Request,
    post_id: Annotated[PublicId, Path(..., description="게시글 공개 ID (Base62)")],
    db: AsyncSession = Depends(get_master_db),
):
    await AdminService.delete_post(post_id, db=db)
    return api_response(request, code=ApiCode.OK, data=None)


@router.patch(
    "/comments/{comment_id}/unblind",
    status_code=200,
    response_model=ApiResponse[UnblindedResponse],
)
async def unblind_comment(
    request: Request,
    comment_id: Annotated[PublicId, Path(..., description="댓글 공개 ID (Base62)")],
    db: AsyncSession = Depends(get_master_db),
):
    await AdminService.unblind(TargetType.COMMENT, comment_id, db=db)
    return api_response(request, code=ApiCode.OK, data=UnblindedResponse())


@router.patch(
    "/comments/{comment_id}/blind",
    status_code=200,
    response_model=ApiResponse[BlindedResponse],
)
async def blind_comment(
    request: Request,
    comment_id: Annotated[PublicId, Path(..., description="댓글 공개 ID (Base62)")],
    db: AsyncSession = Depends(get_master_db),
):
    await AdminService.blind(TargetType.COMMENT, comment_id, db=db)
    return api_response(request, code=ApiCode.OK, data=BlindedResponse())


@router.patch(
    "/comments/{comment_id}/reset-reports",
    status_code=200,
    response_model=ApiResponse[ResetReportsResponse],
)
async def reset_comment_reports(
    request: Request,
    comment_id: Annotated[PublicId, Path(..., description="댓글 공개 ID (Base62)")],
    db: AsyncSession = Depends(get_master_db),
):
    await AdminService.reset_reports(TargetType.COMMENT, comment_id, db=db)
    return api_response(request, code=ApiCode.OK, data=ResetReportsResponse())


@router.delete(
    "/posts/{post_id}/comments/{comment_id}",
    status_code=200,
    response_model=ApiResponse[None],
)
async def delete_comment_admin(
    request: Request,
    post_id: Annotated[PublicId, Path(..., description="게시글 공개 ID (Base62)")],
    comment_id: Annotated[PublicId, Path(..., description="댓글 공개 ID (Base62)")],
    db: AsyncSession = Depends(get_master_db),
):
    await AdminService.delete_comment(post_id, comment_id, db=db)
    return api_response(request, code=ApiCode.OK, data=None)
