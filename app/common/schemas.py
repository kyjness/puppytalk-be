# 공통 스키마·응답 래퍼. BaseSchema, ApiResponse, PaginatedResponse 등.
from typing import Annotated, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer, WithJsonSchema

from app.common.codes import ApiCode
from app.core.ids import parse_optional_public_id_value, parse_public_id_value, uuid_to_base62

PublicId = Annotated[
    UUID,
    BeforeValidator(parse_public_id_value),
    PlainSerializer(lambda u: uuid_to_base62(u), return_type=str),
    WithJsonSchema({"type": "string", "description": "엔티티 공개 ID (Base62)"}),
]

OptionalPublicId = Annotated[
    UUID | None,
    BeforeValidator(parse_optional_public_id_value),
    PlainSerializer(
        lambda u: None if u is None else uuid_to_base62(u),
        return_type=str | None,
    ),
    WithJsonSchema({"type": ["string", "null"]}),
]


def to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0].lower() + "".join(w.capitalize() for w in parts[1:])


class BaseSchema(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        serialize_by_alias=True,
        use_enum_values=True,
    )


T = TypeVar("T")


class ApiResponse(BaseSchema, Generic[T]):
    """code는 ApiCode enum 또는 str. BaseSchema.use_enum_values로 직렬화 시 .value."""

    code: ApiCode | str
    data: T | None = None
    message: str | None = None
    request_id: str = Field(
        default="",
        description="요청 추적 ID(X-Request-ID 헤더와 동일). 에러 토스트·지원 문의용.",
    )


class PaginatedResponse(BaseSchema, Generic[T]):
    items: list[T] = Field(default_factory=list)
    has_more: bool = False
    total: int = 0


class CursorPage(BaseSchema, Generic[T]):
    """커서(무한 스크롤) 페이지. total 없음 — 다음 페이지 존재 여부는 has_more로 표현한다."""

    items: list[T] = Field(default_factory=list)
    has_more: bool = False


def split_page(rows: list[T], size: int) -> tuple[list[T], bool]:
    """size+1 오버페치 결과를 (페이지, has_more)로 가른다 — keyset 페이지네이션 공용."""
    return rows[:size], len(rows) > size


class RootData(BaseSchema):
    message: str = ""
    version: str = ""
    docs: str = ""
