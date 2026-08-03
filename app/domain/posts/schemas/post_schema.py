from typing import Annotated

from pydantic import AfterValidator, AliasChoices, Field, computed_field, field_validator

from app.common import BaseSchema, OptionalPublicId, PublicId, UtcDatetime
from app.common.codes import ApiCode
from app.domain.users.schema import AuthorInfo

_POST_HASHTAGS_MAX = 6
# 게시글 이미지 상한의 단일 정의처 — 초과는 여기서 거부하고, 리포지토리는 검증된 입력을 신뢰한다.
POST_IMAGES_MAX = 5


def _image_ids_max_five(v: list[str] | None) -> list[str] | None:
    if v is not None and len(v) > POST_IMAGES_MAX:
        raise ValueError(ApiCode.POST_FILE_LIMIT_EXCEEDED.name)
    return v


def _hashtags_max_six(v: list[str] | None) -> list[str] | None:
    if v is not None and len(v) > _POST_HASHTAGS_MAX:
        raise ValueError(ApiCode.POST_HASHTAG_LIMIT_EXCEEDED.name)
    return v


ImageIdsMaxFive = Annotated[list[PublicId] | None, AfterValidator(_image_ids_max_five)]
HashtagsMaxSix = Annotated[list[str] | None, AfterValidator(_hashtags_max_six)]


class PostIdData(BaseSchema):
    id: PublicId


class PostCreateRequest(BaseSchema):
    title: str = Field(..., min_length=1, max_length=26)
    content: str = Field(..., min_length=1, max_length=50_000)
    image_ids: ImageIdsMaxFive = None
    category_id: int | None = None
    hashtags: HashtagsMaxSix = None


class PostUpdateRequest(BaseSchema):
    title: str | None = Field(default=None, min_length=1, max_length=26)
    content: str | None = Field(default=None, min_length=1, max_length=50_000)
    image_ids: ImageIdsMaxFive = None
    category_id: int | None = None
    hashtags: HashtagsMaxSix = None
    version: int | None = Field(
        default=None,
        description="낙관적 락: 직전 GET 응답의 version과 일치해야 수정 성공",
    )


class FileInfo(BaseSchema):
    id: PublicId
    file_url: str | None = None
    image_id: OptionalPublicId = None


class PostResponse(BaseSchema):
    id: PublicId
    title: str
    content: str
    view_count: int = 0
    like_count: int = 0
    comment_count: int = 0
    is_liked: bool = False
    # ORM 속성명(user·post_images)과 응답 필드명이 달라 validation_alias로 매핑한다
    # (모델에 직렬화용 @property를 두지 않기 위함). 직렬화명은 alias_generator가 유지.
    author: AuthorInfo | None = Field(default=None, validation_alias=AliasChoices("user", "author"))
    files: list[FileInfo] = Field(
        default_factory=list, validation_alias=AliasChoices("post_images", "files")
    )
    category_id: int | None = None
    hashtags: list[str] = Field(default_factory=list)
    version: int = 1
    created_at: UtcDatetime

    @computed_field
    @property
    def is_edited(self) -> bool:
        return self.version > 1

    @field_validator("hashtags", mode="before")
    @classmethod
    def _hashtags_from_entities(cls, v: object):
        if v is None:
            return []
        if isinstance(v, list):
            if not v:
                return []
            if isinstance(v[0], str):
                return v
            return [getattr(x, "name", str(x)) for x in v]
        return v
