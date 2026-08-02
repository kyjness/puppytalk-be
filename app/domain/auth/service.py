# 인증 비즈니스 로직. 순수 데이터 반환·커스텀 예외. Redis 연동 캡슐화. Full-Async.
# Redis 기반 Refresh Token 저장 + Access Token 블랙리스트(로그아웃). Full-Async.

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import UserStatus
from app.common.exceptions import (
    EmailAlreadyExistsException,
    ForbiddenException,
    InvalidCredentialsException,
    MissingRequiredFieldException,
    NicknameAlreadyExistsException,
    SignupImageTokenInvalidException,
    UnauthorizedException,
)
from app.core.ids import jwt_sub_to_uuid
from app.core.security import (
    access_jti_blacklist_redis_key,
    create_access_token,
    create_refresh_token,
    hash_password,
    password_with_pepper,
    refresh_token_digest,
    verify_password_with_legacy_fallback,
    verify_refresh_token,
)
from app.domain.auth.schema import (
    AccessTokenData,
    LoginRequest,
    LoginSuccessData,
    SignUpRequest,
)
from app.domain.auth.user_status_cache import (
    USER_STATUS_CACHE_TTL_SECONDS,
    invalidate_user_status_cache,
    set_user_status_cache_best_effort,
    user_status_cache_key,
)
from app.domain.media.model import MediaModel
from app.domain.media.service import MediaService
from app.domain.users.model import UsersModel
from app.infra.redis import RedisLike, bulk_to_str

logger = logging.getLogger(__name__)

_USER_REFRESH_KEY_PREFIX = "user_refresh:"

# RTR: 저장된 refresh 다이제스트와 제시된 다이제스트를 비교한 뒤, 일치 시에만 새 다이제스트+TTL을 SET.
# 단일 키만 사용 → Redis Cluster에서도 동일 해시 슬롯으로 스크립트 실행 가능.
# 반환: 1=성공, 0=키 없음·다이제스트 불일치·ttl<=0.
_REFRESH_ROTATE_CAS_LUA = """
local cur = redis.call('GET', KEYS[1])
if not cur then
  return 0
end
if cur ~= ARGV[1] then
  return 0
end
local ttl = tonumber(ARGV[3])
if (not ttl) or ttl <= 0 then
  return 0
end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ttl)
return 1
"""


def _user_refresh_key(user_id: UUID) -> str:
    # JWT sub(Base62/레거시 ULID)와 무관하게 항상 UUID 문자열로 정규화(배포 직후 기존 refresh 키는 무효화됨).
    return f"{_USER_REFRESH_KEY_PREFIX}{user_id}"


async def _refresh_rotation_sequential(
    redis_client: RedisLike,
    *,
    user_key: str,
    incoming_digest: str,
    new_digest: str,
    ttl_seconds: int,
) -> bool:
    """
    Lua eval 실패 시 fail-open용 비원자적 경로(GET → 비교 → SET).

    네트워크/일시 오류 시에만 사용하며, 정상 시에는 `_try_refresh_rotation_redis`의 Lua CAS를 쓴다.
    """
    try:
        raw = await redis_client.get(user_key)
        if bulk_to_str(raw) != incoming_digest:
            return False
        await redis_client.set(user_key, new_digest, ex=ttl_seconds)
        return True
    except Exception as e:
        logger.warning("refresh sequential fallback failed user_key=%s err=%s", user_key, e)
        return False


async def _try_refresh_rotation_redis(
    redis_client: RedisLike,
    *,
    user_id: UUID,
    user_key: str,
    incoming_digest: str,
    new_digest: str,
    ttl_seconds: int,
) -> bool:
    """
    Redis에 기록된 refresh 다이제스트를 RTR 규칙으로 교체한다.

    - ``ttl_seconds > 0``: ``GET`` 비교와 ``SET``을 Lua 한 번에 수행해 경쟁 회전에도 정합성 유지.
    - ``ttl_seconds <= 0``: 저장 갱신 없이(로그인과 동일한 생략 의미) 현재 다이제스트 일치만 검사.
    - eval 예외: 기존 정책에 맞춰 순차 경로로 fail-open(가용성 우선, 완전 원자성은 약화).
    """
    if ttl_seconds <= 0:
        try:
            raw = await redis_client.get(user_key)
        except Exception as e:
            logger.warning("refresh get digest failed user_id=%s err=%s", user_id, e)
            return False
        return bulk_to_str(raw) == incoming_digest

    try:
        rc = await redis_client.eval(
            _REFRESH_ROTATE_CAS_LUA,
            1,
            user_key,
            incoming_digest,
            new_digest,
            str(ttl_seconds),
        )
    except Exception as e:
        logger.warning(
            "refresh CAS eval failed; sequential fallback user_id=%s err=%s",
            user_id,
            e,
        )
        return await _refresh_rotation_sequential(
            redis_client,
            user_key=user_key,
            incoming_digest=incoming_digest,
            new_digest=new_digest,
            ttl_seconds=ttl_seconds,
        )

    try:
        return int(rc) == 1
    except (TypeError, ValueError):
        return False


async def _ensure_user_may_refresh(
    redis_client: RedisLike,
    user_id: UUID,
    db: AsyncSession,
) -> None:
    """
    Refresh 허용 여부: Cache-Aside. Redis 히트 시 DB 생략, 미스·오류 시 DB로 폴백 후 캐시 갱신.

    캐시 값은 ``UserStatus`` 문자열(ACTIVE|SUSPENDED|WITHDRAWN). ACTIVE만 통과, 그 외·무효는 401.
    """
    key = user_status_cache_key(user_id)
    try:
        cached_raw = await redis_client.get(key)
    except Exception as e:
        logger.warning("user status cache GET fail-open user_id=%s err=%s", user_id, e)
        cached_raw = None

    if cached_raw is not None:
        cached = bulk_to_str(cached_raw)
        if cached == UserStatus.ACTIVE.value:
            return
        raise UnauthorizedException(message=UserStatus.inactive_message_ko(cached))

    async with db.begin():
        user = await UsersModel.get_user_by_id(user_id, db=db)
        if not user:
            raise UnauthorizedException()
        status_val = str(user.status)

    try:
        await redis_client.set(key, status_val, ex=USER_STATUS_CACHE_TTL_SECONDS)
    except Exception as e:
        logger.warning("user status cache SET after DB fail-open user_id=%s err=%s", user_id, e)

    if not UserStatus.is_active_value(status_val):
        raise UnauthorizedException(message=UserStatus.inactive_message_ko(status_val))


def _remaining_ttl_seconds_from_access_payload(payload: dict) -> int:
    exp = payload.get("exp")
    now = datetime.now(UTC)
    if isinstance(exp, int):
        exp_dt = datetime.fromtimestamp(exp, UTC)
    elif isinstance(exp, datetime):
        exp_dt = exp if exp.tzinfo is not None else exp.replace(tzinfo=UTC)
    else:
        return 0
    return max(0, int((exp_dt - now).total_seconds()))


class AuthService:
    @classmethod
    async def signup(
        cls, data: SignUpRequest, db: AsyncSession, redis: RedisLike | None = None
    ) -> None:
        has_token = bool(data.signup_token)
        if data.profile_image_id is not None and not has_token:
            # validator: profile_image_id만 단독 전달은 허용하지 않음.
            raise MissingRequiredFieldException()
        hashed = await hash_password(password_with_pepper(data.password))
        async with db.begin():
            if await UsersModel.email_exists(data.email, db=db):
                raise EmailAlreadyExistsException()
            if await UsersModel.nickname_exists(data.nickname, db=db):
                raise NicknameAlreadyExistsException()
            profile_image_id = None
            if has_token:
                image_id_from_token = await MediaService.verify_upload_token(
                    data.signup_token or "", redis=redis
                )
                if image_id_from_token is None:
                    raise SignupImageTokenInvalidException()
                if (
                    data.profile_image_id is not None
                    and data.profile_image_id != image_id_from_token
                ):
                    raise SignupImageTokenInvalidException()
                profile_image_id = image_id_from_token
            try:
                created = await UsersModel.create_user(
                    data.email,
                    hashed,
                    data.nickname,
                    profile_image_id=profile_image_id,
                    db=db,
                )
            except IntegrityError as e:
                # psycopg v3는 pgcode가 아니라 sqlstate를 노출한다(전역 핸들러와 동일 규약).
                orig = getattr(e, "orig", None)
                if getattr(orig, "sqlstate", None) == "23505":
                    constraint = getattr(getattr(orig, "diag", None), "constraint_name", "") or ""
                    if "email" in constraint:
                        raise EmailAlreadyExistsException() from e
                    if "nickname" in constraint:
                        raise NicknameAlreadyExistsException() from e
                raise
            if profile_image_id is not None:
                attached = await MediaModel.claim_image_ownership(
                    profile_image_id, created.id, db=db
                )
                if not attached:
                    # 토큰 경쟁/재사용 등으로 인해 DB 첨부가 실패한 경우.
                    raise SignupImageTokenInvalidException()

    @classmethod
    async def login(
        cls,
        data: LoginRequest,
        db: AsyncSession,
        redis: RedisLike | None = None,
        refresh_ttl_seconds: int = 0,
    ) -> tuple[LoginSuccessData, str]:
        async with db.begin():
            user = await UsersModel.get_user_by_email(data.email, db=db)
            if not user:
                raise InvalidCredentialsException()
            if not UserStatus.is_active_value(user.status):
                raise ForbiddenException()
            if not await verify_password_with_legacy_fallback(data.password, user.password):
                raise InvalidCredentialsException()
            access_token = create_access_token(sub=user.id)
            refresh_token = create_refresh_token(sub=user.id)
            payload = LoginSuccessData(
                id=user.id,
                email=user.email,
                nickname=user.nickname,
                status=UserStatus(user.status),
                profile_image_id=user.profile_image_id,
                profile_image_url=user.profile_image_url,
                access_token=access_token,
            )
        # Refresh Token은 user_refresh:{user_id}에 단일 값으로 저장 (RTR 전제).
        if redis is not None and refresh_ttl_seconds > 0:
            await redis.set(
                _user_refresh_key(payload.id),
                refresh_token_digest(refresh_token),
                ex=refresh_ttl_seconds,
            )
            await set_user_status_cache_best_effort(redis, payload.id, UserStatus.ACTIVE.value)
        return (payload, refresh_token)

    @classmethod
    async def logout(
        cls,
        *,
        user_id: UUID,
        refresh_token: str | None,
        access_payload: dict,
        redis: RedisLike | None = None,
    ) -> None:
        # 운영 관점: 로그아웃은 블랙리스트/refresh 폐기가 핵심이므로 Redis 없이는 실패시키는 편이 안전.
        if redis is None:
            raise UnauthorizedException(
                message="로그아웃을 처리할 수 없습니다. 잠시 후 다시 시도하세요."
            )

        # 1) access jti 블랙리스트 등록(남은 TTL만큼).
        ttl = _remaining_ttl_seconds_from_access_payload(access_payload)
        jti = access_payload.get("jti")
        if ttl > 0 and isinstance(jti, str) and jti.strip():
            await redis.set(access_jti_blacklist_redis_key(jti.strip()), "logout", ex=ttl)

        # 2) refresh token 폐기(회전 전제이므로 user_id 키 삭제).
        await redis.delete(_user_refresh_key(user_id))

    @classmethod
    async def revoke_refresh_for_user(cls, user_id: UUID, redis: RedisLike | None = None) -> None:
        if redis:
            await redis.delete(_user_refresh_key(user_id))

    @classmethod
    async def invalidate_user_status_cache(cls, redis: RedisLike | None, user_id: UUID) -> None:
        """도메인·라우터에서 status 변경 직후 호출. 모듈 함수 ``invalidate_user_status_cache`` 위임."""
        await invalidate_user_status_cache(redis, user_id)

    @classmethod
    async def refresh_tokens(
        cls,
        refresh_token: str | None,
        redis: RedisLike | None,
        db: AsyncSession,
        refresh_ttl_seconds: int,
    ) -> tuple[AccessTokenData, str]:
        """
        Refresh 쿠키 기반 RTR. Redis에 저장된 다이제스트와 제시된 토큰 다이제스트가 일치할 때만 회전.

        유저 활성 여부는 ``user:status:{user_id}`` 캐시(Cache-Aside, 짧은 TTL)로 우선 판단해 DB 조회를 줄인다.
        동시 요청은 Lua CAS로 한 승자만 성공하고, 나머지는 다이제스트 불일치(0)로 처리되어
        ``UnauthorizedException``(401)로 재로그인을 유도한다.
        """
        if not refresh_token:
            raise UnauthorizedException()
        payload = verify_refresh_token(refresh_token)
        sub = payload.get("sub")
        if not isinstance(sub, str) or not sub:
            raise UnauthorizedException()
        try:
            user_uuid = jwt_sub_to_uuid(sub)
        except ValueError:
            raise UnauthorizedException(
                message="인증을 갱신할 수 없습니다. 다시 로그인해 주세요.",
            ) from None
        if redis is None:
            raise UnauthorizedException(
                message="인증을 갱신할 수 없습니다. 잠시 후 다시 시도하세요."
            )
        incoming_digest = refresh_token_digest(refresh_token)
        user_key = _user_refresh_key(user_uuid)

        await _ensure_user_may_refresh(redis, user_uuid, db)

        new_refresh = create_refresh_token(sub=user_uuid)
        new_digest = refresh_token_digest(new_refresh)

        rotated = await _try_refresh_rotation_redis(
            redis,
            user_id=user_uuid,
            user_key=user_key,
            incoming_digest=incoming_digest,
            new_digest=new_digest,
            ttl_seconds=refresh_ttl_seconds,
        )
        if not rotated:
            raise UnauthorizedException(
                message="인증을 갱신할 수 없습니다. 다시 로그인해 주세요.",
            )

        new_access = create_access_token(sub=user_uuid)
        return AccessTokenData(access_token=new_access), new_refresh
