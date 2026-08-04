# 사용자 요청/응답 DTO. 닉네임·비밀번호 검증은 상단 헬퍼 + Annotated로 응집.

import re
from typing import Annotated, Any

from pydantic import AfterValidator, Field, field_validator, model_validator

from app.common import BaseSchema, OptionalPublicId, PublicId, UserStatus, UtcDatetime
from app.common.codes import ApiCode
from app.domain.dogs.schema import (
    DogProfileResponse,
    DogProfileUpsertItem,
    RepresentativeDogInfo,
)

# --- 1. 상수 ---
_PASSWORD_SPECIAL = re.compile(r"[!@#$%^&*()_+\-=\[\]{};\':\"\\|,.<>/?]")
_NICKNAME_PATTERN = re.compile(r"^[가-힣a-zA-Z0-9]{1,10}$")

# --- 2. 내부 헬퍼 (스키마 전용) ---


def _password_complexity_ok(password: str) -> bool:
    return (
        bool(re.search(r"[a-z]", password))
        and bool(re.search(r"[0-9]", password))
        and bool(_PASSWORD_SPECIAL.search(password))
    )


def _validate_password_format_auth(password: str) -> bool:
    if not password or not isinstance(password, str):
        return False
    if len(password) < 8 or len(password) > 20:
        return False
    return _password_complexity_ok(password)


def _validate_password_format_update(password: str) -> bool:
    if not password or not isinstance(password, str):
        return False
    if len(password) < 8 or len(password) > 128:
        return False
    return _password_complexity_ok(password)


def _validate_nickname_format(nickname: str) -> bool:
    return bool(nickname and isinstance(nickname, str) and _NICKNAME_PATTERN.match(nickname))


def _ensure_password_format_auth(v: str) -> str:
    if not _validate_password_format_auth(v):
        raise ValueError(ApiCode.INVALID_PASSWORD_FORMAT.name)
    return v


def _ensure_password_format_update(v: str) -> str:
    if not _validate_password_format_update(v):
        raise ValueError(ApiCode.INVALID_PASSWORD_FORMAT.name)
    return v


def _ensure_nickname_format(v: str) -> str:
    if not v or not v.strip():
        raise ValueError(ApiCode.INVALID_REQUEST_BODY.name)
    if not _validate_nickname_format(v.strip()):
        raise ValueError(ApiCode.INVALID_REQUEST_BODY.name)
    return v.strip()


def _optional_nickname(v: str | None) -> str | None:
    if v is None:
        return None
    s = v.strip() if isinstance(v, str) else v
    if not s:
        return None
    return _ensure_nickname_format(s)


# --- 3. Annotated 재사용 타입 ---
PasswordStr = Annotated[str, AfterValidator(_ensure_password_format_auth)]
PasswordUpdateStr = Annotated[str, AfterValidator(_ensure_password_format_update)]
NicknameStr = Annotated[str, AfterValidator(_ensure_nickname_format)]
OptionalNicknameStr = Annotated[str | None, AfterValidator(_optional_nickname)]

# --- 4. 스키마 모델 ---


class AuthorInfo(BaseSchema):
    """게시글·댓글 공용 작성자 표시 스키마. 탈퇴자 익명화 정책의 단일 정의처."""

    id: PublicId
    nickname: str
    profile_image_id: OptionalPublicId = None
    profile_image_url: str | None = None
    representative_dog: RepresentativeDogInfo | None = None

    @model_validator(mode="wrap")
    @classmethod
    def anonymize_inactive(cls, data, handler):
        status = getattr(data, "status", None)
        if status is not None and not UserStatus.is_active_value(status):
            if hasattr(data, "id"):
                return handler(
                    {
                        "id": data.id,
                        "nickname": "알수없음",
                        "profile_image_id": None,
                        "profile_image_url": None,
                        "representative_dog": None,
                    }
                )
        return handler(data)


class AvailabilityData(BaseSchema):
    email_available: bool | None = None
    nickname_available: bool | None = None


class BlockedUserItem(BaseSchema):
    id: PublicId
    nickname: str
    profile_image_url: str | None = None


class BlockToggleResponse(BaseSchema):
    blocked: bool


class UserAvailabilityQuery(BaseSchema):
    email: str | None = None
    nickname: OptionalNicknameStr = None

    @field_validator("email", "nickname", mode="before")
    @classmethod
    def strip_empty_to_none(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip() or None
        return v

    @model_validator(mode="after")
    def at_least_one(self) -> "UserAvailabilityQuery":
        if not (self.email or self.nickname):
            raise ValueError(ApiCode.INVALID_REQUEST.name)
        return self


class UpdateUserRequest(BaseSchema):
    nickname: OptionalNicknameStr = None
    profile_image_id: OptionalPublicId = Field(
        default=None, description="null이면 프로필 이미지 제거"
    )
    clear_profile_image: bool = Field(
        default=False,
        description="프로필 이미지 강제 삭제 플래그 (camelCase: clearProfileImage)",
    )
    dogs: list[DogProfileUpsertItem] | None = Field(
        default=None, description="강아지 목록 전체 교체(생성/수정/삭제 반영)"
    )

    @model_validator(mode="after")
    def at_least_one(self) -> "UpdateUserRequest":
        has_nickname = self.nickname is not None
        has_profile_image_field = "profile_image_id" in self.model_fields_set
        has_dogs = self.dogs is not None
        if has_nickname or has_profile_image_field or has_dogs or self.clear_profile_image:
            return self
        raise ValueError(ApiCode.MISSING_REQUIRED_FIELD.name)


class UpdatePasswordRequest(BaseSchema):
    current_password: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="현재 비밀번호 (DoS 방지 128자 제한)",
    )
    new_password: PasswordUpdateStr = Field(
        ...,
        min_length=8,
        max_length=128,
        description="새 비밀번호 (8~128자, 형식 검사)",
    )


class UserProfileResponse(BaseSchema):
    id: PublicId
    email: str
    nickname: str
    role: str = Field(default="USER", description="USER|ADMIN")
    status: UserStatus = Field(..., description="ACTIVE|SUSPENDED|WITHDRAWN")
    profile_image_id: OptionalPublicId = None
    profile_image_url: str | None = None
    created_at: UtcDatetime
    dogs: list[DogProfileResponse] = Field(default_factory=list, description="등록된 강아지 목록")
    representative_dog: RepresentativeDogInfo | None = None
