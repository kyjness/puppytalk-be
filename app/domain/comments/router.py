# 댓글 라우터. Router → Service. 예외는 전역 handler 처리.

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    CommentAuthorContext,
    CurrentUser,
    get_current_user,
    get_current_user_optional,
    get_master_db,
    get_optional_redis,
    get_slave_db,
    require_comment_author,
    require_comment_author_for_delete,
)
from app.common import (
    ApiCode,
    ApiResponse,
    CursorPage,
    OptionalPublicId,
    PublicId,
    api_response,
)
from app.domain.comments.schema import (
    CommentCreateRequest,
    CommentIdData,
    CommentResponse,
    CommentUpdateRequest,
)
from app.domain.comments.service import CommentService
from app.infra.redis import RedisLike

router = APIRouter(prefix="/posts/{post_id}/comments", tags=["comments"])


@router.post("", status_code=201, response_model=ApiResponse[CommentIdData])
async def create_comment(
    request: Request,
    comment_data: CommentCreateRequest,
    post_id: Annotated[PublicId, Path(..., description="게시글 공개 ID (Base62)")],
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_master_db),
    redis: RedisLike | None = Depends(get_optional_redis),
):
    data = await CommentService.create_comment(post_id, user.id, comment_data, db=db, redis=redis)
    return api_response(request, code=ApiCode.OK, data=data)


@router.get("", status_code=200, response_model=ApiResponse[CursorPage[CommentResponse]])
async def get_comments(
    request: Request,
    post_id: Annotated[PublicId, Path(..., description="게시글 공개 ID (Base62)")],
    cursor: Annotated[
        OptionalPublicId,
        Query(
            description="무한 스크롤: 직전 응답의 마지막 루트 댓글 id(공개 ID). 미지정 시 처음부터."
        ),
    ] = None,
    size: int = Query(10, ge=1, le=100, description="페이지 크기"),
    sort: str | None = Query(None, description="정렬: latest|oldest"),
    db: AsyncSession = Depends(get_slave_db),
    current_user: CurrentUser | None = Depends(get_current_user_optional),
):
    result, has_more = await CommentService.get_comments(
        post_id,
        size,
        db=db,
        sort=sort,
        cursor=cursor,
        current_user_id=current_user.id if current_user else None,
    )
    return api_response(request, code=ApiCode.OK, data=CursorPage(items=result, has_more=has_more))


@router.patch("/{comment_id}", status_code=200, response_model=ApiResponse[None])
async def update_comment(
    request: Request,
    comment_data: CommentUpdateRequest,
    author_ctx: CommentAuthorContext = Depends(require_comment_author),
    db: AsyncSession = Depends(get_master_db),
):
    await CommentService.update_comment(
        author_ctx.post_id, author_ctx.comment_id, comment_data, db=db
    )
    return api_response(request, code=ApiCode.OK, data=None)


@router.delete("/{comment_id}", status_code=200, response_model=ApiResponse[None])
async def delete_comment(
    request: Request,
    author_ctx: CommentAuthorContext = Depends(require_comment_author_for_delete),
    db: AsyncSession = Depends(get_master_db),
):
    await CommentService.delete_comment(author_ctx.post_id, author_ctx.comment_id, db=db)
    return api_response(request, code=ApiCode.OK, data=None)
