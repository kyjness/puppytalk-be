# 사용자 라우터. Router → Service. 예외는 전역 handler 처리.
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    CurrentUser,
    get_current_user,
    get_master_db,
    get_slave_db,
    parse_availability_query,
)
from app.common import (
    ApiCode,
    ApiResponse,
    CursorPage,
    OptionalPublicId,
    PublicId,
    api_response,
)
from app.domain.auth.service import AuthService
from app.domain.users.schema import (
    AvailabilityData,
    BlockedUserItem,
    BlockToggleResponse,
    UpdatePasswordRequest,
    UpdateUserRequest,
    UserAvailabilityQuery,
    UserProfileResponse,
)
from app.domain.users.service import UserService
from app.infra.redis import get_app_redis

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/availability", status_code=200, response_model=ApiResponse[AvailabilityData])
async def check_availability(
    request: Request,
    query: UserAvailabilityQuery = Depends(parse_availability_query),
    db: AsyncSession = Depends(get_slave_db),
):
    data = await UserService.check_availability(query, db=db)
    return api_response(request, code=ApiCode.OK, data=data)


@router.get("/me", status_code=200, response_model=ApiResponse[UserProfileResponse])
async def get_me(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_slave_db),
):
    data = await UserService.get_user_profile(user.id, db=db)
    return api_response(request, code=ApiCode.OK, data=data)


@router.patch("/me", status_code=200, response_model=ApiResponse[UserProfileResponse])
async def update_me(
    request: Request,
    user_data: UpdateUserRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_master_db),
):
    data = await UserService.update_user_profile(user.id, user_data, db=db)
    # 닉네임·프로필 이미지는 인증 스냅샷 캐시(CurrentUser)에 실린다 — 변경 즉시 무효화.
    await AuthService.invalidate_user_status_cache(get_app_redis(request.app), user.id)
    return api_response(request, code=ApiCode.OK, data=data)


@router.patch("/me/password", status_code=200, response_model=ApiResponse[None])
async def update_password(
    request: Request,
    password_data: UpdatePasswordRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_master_db),
):
    await UserService.update_password(user.id, password_data, db=db)
    redis = get_app_redis(request.app)
    await AuthService.revoke_refresh_for_user(user.id, redis)
    return api_response(request, code=ApiCode.OK, data=None)


@router.delete("/me", status_code=200, response_model=ApiResponse[None])
async def delete_me(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_master_db),
):
    redis = get_app_redis(request.app)
    await AuthService.revoke_refresh_for_user(user.id, redis)
    await UserService.delete_user(user.id, db=db)
    await AuthService.invalidate_user_status_cache(redis, user.id)
    return api_response(request, code=ApiCode.OK, data=None)


@router.get("/me/blocks", status_code=200, response_model=ApiResponse[CursorPage[BlockedUserItem]])
async def get_my_blocks(
    request: Request,
    cursor: Annotated[
        OptionalPublicId,
        Query(
            description="무한 스크롤: 직전 응답의 마지막 차단 유저 id(공개 ID). 미지정 시 처음부터."
        ),
    ] = None,
    size: int = Query(20, ge=1, le=100, description="페이지 크기"),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_slave_db),
):
    items, has_more = await UserService.get_blocked_list(user.id, db=db, size=size, cursor=cursor)
    return api_response(request, code=ApiCode.OK, data=CursorPage(items=items, has_more=has_more))


@router.post(
    "/{target_user_id}/block",
    status_code=200,
    response_model=ApiResponse[BlockToggleResponse],
)
async def toggle_block_user(
    request: Request,
    target_user_id: Annotated[PublicId, Path(..., description="대상 사용자 공개 ID (Base62)")],
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_master_db),
):
    """유저 차단/차단해제 토글. 이미 차단된 경우 해제."""
    is_blocked = await UserService.toggle_block_user(user.id, target_user_id, db=db)
    return api_response(
        request,
        code=ApiCode.OK,
        data=BlockToggleResponse(blocked=is_blocked),
    )
