# 인증 의존성. Authorization Bearer 검증 → CurrentUser. Full-Async.

import logging

import jwt
from fastapi import Depends, Request
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.common import BaseSchema, OptionalPublicId, PublicId, UserStatus, UtcDatetime
from app.common.exceptions import ForbiddenException, UnauthorizedException
from app.core.ids import jwt_sub_to_uuid
from app.core.security import access_jti_blacklist_redis_key, verify_access_token
from app.db import utc_now
from app.domain.auth.user_status_cache import (
    set_user_status_cache_best_effort,
    user_status_cache_key,
)
from app.domain.users.model import UsersModel
from app.infra.redis import bulk_to_str, get_app_redis

from .db import get_slave_db

logger = logging.getLogger(__name__)


class CurrentUser(BaseSchema):
    id: PublicId = Field(..., description="사용자 공개 ID (Base62)")
    email: str = ""
    nickname: str = ""
    role: str | None = Field(default="USER", description="USER|ADMIN")
    profile_image_id: OptionalPublicId = None
    profile_image_url: str | None = None
    created_at: UtcDatetime = Field(default_factory=utc_now)


def _bearer_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        return None
    return auth[7:].strip() or None


async def _ensure_jti_not_blacklisted(jti: str, request: Request) -> None:
    """Access Token jti 블랙리스트(로그아웃). Redis 장애 시 Fail-open."""
    redis_client = get_app_redis(request.app)
    if redis_client is None:
        return
    key = access_jti_blacklist_redis_key(jti)
    try:
        if await redis_client.get(key) is not None:
            raise UnauthorizedException(message="인증 토큰이 유효하지 않습니다.")
    except UnauthorizedException:
        raise
    except Exception:
        return


async def get_current_user_optional(
    request: Request,
    db: AsyncSession = Depends(get_slave_db),
) -> CurrentUser | None:
    token = _bearer_token(request)
    if not token:
        return None
    try:
        payload = verify_access_token(token)
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None
    jti = payload.get("jti")
    if isinstance(jti, str) and jti.strip():
        try:
            await _ensure_jti_not_blacklisted(jti.strip(), request)
        except UnauthorizedException:
            return None
    sub = payload.get("sub")
    if sub is None:
        return None
    if not isinstance(sub, str):
        return None
    try:
        user_id = jwt_sub_to_uuid(sub)
    except ValueError:
        return None
    async with db.begin():
        user = await UsersModel.get_user_by_id(user_id, db=db)
        if not user or not UserStatus.is_active_value(user.status):
            return None
        return CurrentUser.model_validate(user)


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_slave_db),
) -> CurrentUser:
    token = _bearer_token(request)
    if not token:
        raise UnauthorizedException(message="로그인이 필요합니다.")
    try:
        payload = verify_access_token(token)
    except jwt.ExpiredSignatureError:
        raise UnauthorizedException(message="인증 토큰이 유효하지 않습니다.")
    except jwt.InvalidTokenError:
        raise UnauthorizedException(message="인증 토큰이 유효하지 않습니다.")
    jti = payload.get("jti")
    if isinstance(jti, str) and jti.strip():
        await _ensure_jti_not_blacklisted(jti.strip(), request)
    sub = payload.get("sub")
    if sub is None:
        raise UnauthorizedException(message="인증 토큰이 유효하지 않습니다.")
    if not isinstance(sub, str):
        raise UnauthorizedException(message="인증 토큰이 유효하지 않습니다.")
    try:
        user_id = jwt_sub_to_uuid(sub)
    except ValueError:
        raise UnauthorizedException(message="인증 토큰이 유효하지 않습니다.") from None

    # refresh_tokens와 동일한 user:status 캐시(키·TTL 공유)로 정지/탈퇴 사용자를 fast-fail.
    # CurrentUser는 email/nickname/role 등 전체 row가 필요해 ACTIVE 히트 시에도 DB 조회는 유지.
    redis_client = get_app_redis(request.app)
    cached_status: str | None = None
    if redis_client is not None:
        try:
            cached_raw = await redis_client.get(user_status_cache_key(user_id))
            cached_status = bulk_to_str(cached_raw)
        except Exception as e:
            logger.warning("user status cache GET fail-open user_id=%s err=%s", user_id, e)
            cached_status = None
        if cached_status is not None and not UserStatus.is_active_value(cached_status):
            raise ForbiddenException(message=UserStatus.inactive_message_ko(cached_status))

    async with db.begin():
        user = await UsersModel.get_user_by_id(user_id, db=db)
        if not user:
            raise UnauthorizedException(message="인증 토큰이 유효하지 않습니다.")
        status_val = str(user.status)
        result = CurrentUser.model_validate(user)

    if redis_client is not None and cached_status is None:
        await set_user_status_cache_best_effort(redis_client, user_id, status_val)

    if not UserStatus.is_active_value(status_val):
        raise ForbiddenException(message=UserStatus.inactive_message_ko(status_val))
    return result


async def get_current_admin(
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    if getattr(current_user, "role", None) != "ADMIN":
        raise ForbiddenException(message="관리자 권한이 필요합니다.")
    return current_user
