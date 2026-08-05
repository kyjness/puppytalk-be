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
    PaginatedResponse,
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


@router.get("", status_code=200, response_model=ApiResponse[PaginatedResponse[CommentResponse]])
async def get_comments(
    request: Request,
    post_id: Annotated[PublicId, Path(..., description="게시글 공개 ID (Base62)")],
    page: int = Query(1, ge=1, description="1-base 페이지 번호"),
    size: int = Query(10, ge=1, le=100, description="페이지 크기"),
    sort: str | None = Query(None, description="정렬: latest|popular"),
    db: AsyncSession = Depends(get_slave_db),
    current_user: CurrentUser | None = Depends(get_current_user_optional),
):
    """루트 댓글 목록 — 커서가 아니라 offset+total이다(ADR 0016).

    인기순은 정렬 축(`like_count`)이 변동값이라 keyset이 성립하지 않고, 댓글은 게시글
    1건에 국한된 유한 집합이라 깊은 offset이 실질 문제가 되지 않는다. 대댓글
    ("/{comment_id}/replies")은 축이 불변이라 커서를 유지한다.
    """
    result, total = await CommentService.get_comments(
        post_id,
        page,
        size,
        db=db,
        sort=sort,
        current_user_id=current_user.id if current_user else None,
    )
    return api_response(
        request,
        code=ApiCode.OK,
        data=PaginatedResponse.from_page(result, page=page, size=size, total=total),
    )


@router.get(
    "/{comment_id}/replies",
    status_code=200,
    response_model=ApiResponse[CursorPage[CommentResponse]],
)
async def get_replies(
    request: Request,
    post_id: Annotated[PublicId, Path(..., description="게시글 공개 ID (Base62)")],
    comment_id: Annotated[PublicId, Path(..., description="루트 댓글 공개 ID (Base62)")],
    cursor: Annotated[
        OptionalPublicId,
        Query(
            description="무한 스크롤: 직전 응답의 마지막 대댓글 id(공개 ID). 미지정 시 처음부터."
        ),
    ] = None,
    size: int = Query(10, ge=1, le=100, description="페이지 크기"),
    sort: str | None = Query(None, description="정렬: latest|oldest"),
    db: AsyncSession = Depends(get_slave_db),
    current_user: CurrentUser | None = Depends(get_current_user_optional),
):
    """목록 응답의 대댓글 preview 뒤를 이어 받는다(has_more_replies가 true일 때)."""
    result, has_more = await CommentService.get_replies(
        post_id,
        comment_id,
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
