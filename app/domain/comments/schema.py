# 댓글 요청/응답 DTO. 작성자 표시(익명화 포함)는 users의 AuthorInfo 공용.

from pydantic import Field

from app.common import BaseSchema, OptionalPublicId, PublicId, UtcDatetime
from app.domain.users.schema import AuthorInfo


class CommentIdData(BaseSchema):
    id: PublicId


class CommentCreateRequest(BaseSchema):
    content: str = Field(..., min_length=1, max_length=500, description="댓글 내용 (1~500자)")
    parent_id: OptionalPublicId = None


class CommentUpdateRequest(BaseSchema):
    # 수정은 내용만 — parent_id를 받으면 조용히 무시되는 계약이 생기므로 필드 자체를 두지 않는다.
    content: str = Field(..., min_length=1, max_length=500, description="댓글 내용 (1~500자)")


class CommentResponse(BaseSchema):
    id: PublicId
    content: str
    author: AuthorInfo | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    post_id: OptionalPublicId = None
    parent_id: OptionalPublicId = None
    like_count: int = 0
    is_liked: bool = False
    is_edited: bool = False
    is_deleted: bool = False
    # 전량이 아니라 **preview**다 — 루트당 상위 REPLY_PREVIEW_LIMIT건만 담긴다.
    # 나머지는 GET /posts/{post_id}/comments/{comment_id}/replies 로 이어 받는다.
    replies: list["CommentResponse"] = Field(default_factory=list)
    reply_count: int = 0
    has_more_replies: bool = False


CommentResponse.model_rebuild()
