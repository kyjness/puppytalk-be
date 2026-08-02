# 비밀번호 해시·검증(bcrypt), JWT Access/Refresh 토큰 생성·검증.

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import bcrypt
import jwt
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.ids import new_ulid_str, uuid_to_base62


def password_with_pepper(plain: str) -> str:
    return f"{plain}{settings.PASSWORD_PEPPER}"


def access_jti_blacklist_redis_key(jti: str) -> str:
    return f"blacklist:jti:{jti}"


def refresh_token_digest(refresh_token: str) -> str:
    return hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()


def _hash_password_sync(password: str) -> str:
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")


def _verify_password_sync(password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"),
            hashed_password.encode("utf-8"),
        )
    except (ValueError, TypeError):
        return False


async def hash_password(password: str) -> str:
    return await run_in_threadpool(_hash_password_sync, password)


async def verify_password(password: str, hashed_password: str) -> bool:
    return await run_in_threadpool(_verify_password_sync, password, hashed_password)


async def verify_password_with_legacy_fallback(plain: str, hashed_password: str) -> bool:
    """Pepper 적용 검증 후, 기존 사용자용으로 미적용 평문 검증."""
    if not settings.PASSWORD_PEPPER:
        return await verify_password(plain, hashed_password)
    if await verify_password(password_with_pepper(plain), hashed_password):
        return True
    return await verify_password(plain, hashed_password)


def _now_utc() -> datetime:
    return datetime.now(UTC)


def create_access_token(sub: UUID) -> str:
    """sub=user_id(Base62 공개 ID). jti는 비엔티티 식별용 ULID 유지."""
    expire = _now_utc() + timedelta(seconds=settings.ACCESS_TOKEN_EXPIRE_SECONDS)
    payload = {
        "sub": uuid_to_base62(sub),
        "exp": expire,
        "iat": _now_utc(),
        "type": "access",
        "jti": new_ulid_str(),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(sub: UUID) -> str:
    expire = _now_utc() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub": uuid_to_base62(sub),
        "exp": expire,
        "iat": _now_utc(),
        "type": "refresh",
        "jti": new_ulid_str(),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def verify_access_token(token: str) -> dict[str, Any]:
    """만료/서명 실패 시 ExpiredSignatureError 또는 InvalidTokenError."""
    payload = jwt.decode(
        token,
        settings.JWT_SECRET_KEY,
        algorithms=[settings.JWT_ALGORITHM],
    )
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("invalid token type")
    return payload


def verify_refresh_token(token: str) -> dict[str, Any]:
    """type=refresh 검사. 만료/서명 실패 시 예외."""
    payload = jwt.decode(
        token,
        settings.JWT_SECRET_KEY,
        algorithms=[settings.JWT_ALGORITHM],
    )
    if payload.get("type") != "refresh":
        raise jwt.InvalidTokenError("invalid token type")
    return payload
