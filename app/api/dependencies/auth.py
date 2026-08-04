# 인증 의존성. Authorization Bearer 검증 → CurrentUser. Full-Async.

import asyncio
import logging

import jwt
from fastapi import Depends, Request
from pydantic import Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common import BaseSchema, OptionalPublicId, PublicId, UserStatus, UtcDatetime
from app.common.exceptions import ForbiddenException, UnauthorizedException
from app.core.ids import jwt_sub_to_uuid
from app.core.security import access_jti_blacklist_redis_key, verify_access_token
from app.db import utc_now
from app.domain.auth.user_status_cache import (
    get_auth_cache,
    set_auth_cache_best_effort,
    set_user_status_cache_best_effort,
)
from app.domain.users.model import UsersRepository
from app.infra.redis import RedisLike, get_app_redis

from .db import get_slave_db

logger = logging.getLogger(__name__)

_INVALID_TOKEN_MESSAGE = "인증 토큰이 유효하지 않습니다."


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


async def _ensure_jti_not_blacklisted(jti: str, redis_client: RedisLike | None) -> None:
    """Access Token jti 블랙리스트(로그아웃). Redis 장애 시 Fail-open."""
    if redis_client is None:
        return
    try:
        blacklisted = await redis_client.get(access_jti_blacklist_redis_key(jti)) is not None
    except Exception:
        return
    if blacklisted:
        raise UnauthorizedException(message=_INVALID_TOKEN_MESSAGE)


async def resolve_access_token_user(
    token: str,
    *,
    db: AsyncSession,
    redis_client: RedisLike | None,
) -> CurrentUser:
    """액세스 토큰 → CurrentUser 단일 파이프라인(HTTP 필수/선택·WS 인증 공용).

    검증 → jti 블랙리스트 → sub → 인증 스냅샷 캐시(히트 시 DB 0회) → DB 폴백 순.
    실패는 UnauthorizedException(토큰·사용자 부재)/ForbiddenException(비활성)으로 던지고,
    표면별 응답 형태(401/403 vs WS close code)는 호출부가 정한다.
    """
    try:
        payload = verify_access_token(token)
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        raise UnauthorizedException(message=_INVALID_TOKEN_MESSAGE) from None
    jti = payload.get("jti")
    if isinstance(jti, str) and jti.strip():
        await _ensure_jti_not_blacklisted(jti.strip(), redis_client)
    sub = payload.get("sub")
    if not isinstance(sub, str):
        raise UnauthorizedException(message=_INVALID_TOKEN_MESSAGE)
    try:
        user_id = jwt_sub_to_uuid(sub)
    except ValueError:
        raise UnauthorizedException(message=_INVALID_TOKEN_MESSAGE) from None

    # 인증 스냅샷 캐시: 히트면 정지 fast-fail과 CurrentUser 조립까지 DB 왕복 없이 끝난다.
    if redis_client is not None:
        cached = await get_auth_cache(redis_client, user_id)
        if cached is not None and isinstance(cached.get("status"), str):
            status_val = cached["status"]
            if not UserStatus.is_active_value(status_val):
                raise ForbiddenException(message=UserStatus.inactive_message_ko(status_val))
            try:
                return CurrentUser.model_validate(cached.get("user"))
            except ValidationError:
                pass  # 형식 불일치(배포 중 구버전 값 등)는 미스 취급 — 아래 DB 경로로

    async with db.begin():
        user = await UsersRepository.get_user_by_id(user_id, db=db)
        if not user:
            raise UnauthorizedException(message=_INVALID_TOKEN_MESSAGE)
        status_val = str(user.status)
        result = CurrentUser.model_validate(user)

    if redis_client is not None:
        # 미스 경로 한정 best-effort 채움 — 비활성도 저장해 다음 요청을 fast-fail.
        # user:status(refresh 경로 공유)도 함께 덥힌다. 서로 독립·각자 실패 삼킴 → 병렬.
        await asyncio.gather(
            set_auth_cache_best_effort(
                redis_client,
                user_id,
                status_value=status_val,
                user_payload=result.model_dump(mode="json", by_alias=True),
            ),
            set_user_status_cache_best_effort(redis_client, user_id, status_val),
        )

    if not UserStatus.is_active_value(status_val):
        raise ForbiddenException(message=UserStatus.inactive_message_ko(status_val))
    return result


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_slave_db),
) -> CurrentUser:
    token = _bearer_token(request)
    if not token:
        raise UnauthorizedException(message="로그인이 필요합니다.")
    return await resolve_access_token_user(token, db=db, redis_client=get_app_redis(request.app))


async def get_current_user_optional(
    request: Request,
    db: AsyncSession = Depends(get_slave_db),
) -> CurrentUser | None:
    """토큰 없음·무효·비활성 전부 None(게스트로 계속) — 필수 경로와 같은 파이프라인이라
    캐시 적용 여부도 비대칭이 없다."""
    token = _bearer_token(request)
    if not token:
        return None
    try:
        return await resolve_access_token_user(
            token, db=db, redis_client=get_app_redis(request.app)
        )
    except (UnauthorizedException, ForbiddenException):
        return None


async def get_current_admin(
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    if getattr(current_user, "role", None) != "ADMIN":
        raise ForbiddenException(message="관리자 권한이 필요합니다.")
    return current_user
