# 미디어 라우터. Router → Service. 예외는 전역 handler 처리.
# 업로드는 presigned 3단(presign → S3 presigned POST 직접 업로드 → confirm) 단일 경로 —
# 서버가 파일 본문을 받지 않으므로 업로드 대역폭·메모리가 앱 인스턴스를 거치지 않는다.
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    CurrentUser,
    get_current_user,
    get_master_db,
)
from app.common import ApiCode, ApiResponse, PublicId, api_response
from app.common.exceptions import TooManyRequestsException
from app.core.config import settings
from app.core.rate_limit import check_fixed_window
from app.domain.media.schema import (
    ConfirmSignupUploadRequest,
    ConfirmUploadRequest,
    ImageUploadResponse,
    PresignUploadRequest,
    PresignUploadResponse,
    SignupImageUploadData,
)
from app.domain.media.service import MediaService
from app.infra.redis import RedisLike, get_app_redis

router = APIRouter(prefix="/media", tags=["media"])


@router.post(
    "/images/presign",
    status_code=200,
    response_model=ApiResponse[PresignUploadResponse],
)
async def presign_image_upload(
    request: Request,
    body: PresignUploadRequest,
    user: CurrentUser = Depends(get_current_user),
):
    # 인증 presign은 IP 미들웨어(글로벌 100/분)만으로는 pending/ 대량 적재를 못 막는다 —
    # WS DM과 동형으로 유저 단위 한도(Redis 공유, 장애 시 메모리 폴백). confirm은 유효한
    # 1회성 pending 키가 선행돼야 하므로 비용 원점인 presign만 조인다.
    redis: RedisLike | None = get_app_redis(request.app)
    allowed, retry_after = await check_fixed_window(
        redis,
        f"media_presign:{user.id}",
        window_sec=settings.MEDIA_PRESIGN_RATE_LIMIT_WINDOW,
        max_count=settings.MEDIA_PRESIGN_RATE_LIMIT_MAX,
    )
    if not allowed:
        raise TooManyRequestsException(retry_after_seconds=retry_after)
    data = await MediaService.issue_presigned_upload(body)
    return api_response(request, code=ApiCode.OK, data=data)


@router.post(
    "/images/signup/presign",
    status_code=200,
    response_model=ApiResponse[PresignUploadResponse],
)
async def presign_signup_image_upload(
    request: Request,
    body: PresignUploadRequest,
):
    data = await MediaService.issue_presigned_upload(body)
    return api_response(request, code=ApiCode.OK, data=data)


@router.post(
    "/images/confirm",
    status_code=201,
    response_model=ApiResponse[ImageUploadResponse],
)
async def confirm_image_upload(
    request: Request,
    body: ConfirmUploadRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_master_db),
):
    data = await MediaService.confirm_presigned_upload(body, user.id, db=db)
    return api_response(request, code=ApiCode.OK, data=data)


@router.post(
    "/images/signup/confirm",
    status_code=201,
    response_model=ApiResponse[SignupImageUploadData],
)
async def confirm_signup_image_upload(
    request: Request,
    body: ConfirmSignupUploadRequest,
    db: AsyncSession = Depends(get_master_db),
):
    redis: RedisLike | None = get_app_redis(request.app)
    data = await MediaService.confirm_presigned_signup_upload(body, db=db, redis=redis)
    return api_response(request, code=ApiCode.OK, data=data)


@router.delete("/images/{image_id}", status_code=200, response_model=ApiResponse[None])
async def delete_image(
    request: Request,
    image_id: Annotated[PublicId, Path(..., description="이미지 공개 ID (Base62)")],
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_master_db),
):
    await MediaService.delete_image(image_id, user.id, db=db)
    return api_response(request, code=ApiCode.OK, data=None)
