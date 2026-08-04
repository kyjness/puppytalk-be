"""탈퇴 유저 하드 삭제 시 비정규화 카운트 정합성 통합 테스트.

post_likes/comment_likes.user_id는 ON DELETE CASCADE이고 posts.user_id/comments.author_id는
SET NULL이다 — 유저를 지우면 좋아요 **행만** 사라지고 글·댓글은 남는다. 삭제 전에 like_count를
깎지 않으면 카운트가 영구히 부풀어 있게 되고 자가 치유되지 않는다. 실 Postgres(TEST_DB_URL) 필요.

ORM 인스턴스는 커밋·퍼지 이후 만료될 수 있어, 검증은 미리 확보한 id로 다시 조회해서 한다.
"""

from datetime import timedelta
from uuid import UUID

import pytest
from app.common import UserStatus
from app.db.base_class import utc_now
from app.domain.comments.model import Comment, CommentLike
from app.domain.likes.model import PostLike
from app.domain.posts.model import Post
from app.domain.users.model import User
from app.domain.users.service import UserService
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


async def _mk_user(
    db: AsyncSession, email: str, nickname: str, *, withdrawn_days: int | None = None
) -> UUID:
    now = utc_now()
    u = User(
        email=email,
        password="x" * 60,
        nickname=nickname,
        status=(UserStatus.WITHDRAWN if withdrawn_days is not None else UserStatus.ACTIVE).value,
        deleted_at=(now - timedelta(days=withdrawn_days)) if withdrawn_days is not None else None,
        created_at=now,
        updated_at=now,
    )
    db.add(u)
    await db.flush()
    return u.id


async def _mk_post(db: AsyncSession, author_id: UUID, title: str, *, like_count: int) -> UUID:
    now = utc_now()
    p = Post(
        user_id=author_id,
        title=title,
        content="본문",
        like_count=like_count,
        created_at=now,
        updated_at=now,
    )
    db.add(p)
    await db.flush()
    return p.id


async def _count(db: AsyncSession, model, cond) -> int:
    return (await db.execute(select(func.count()).select_from(model).where(cond))).scalar_one()


async def _like_count(db: AsyncSession, model, ident: UUID) -> int:
    return (await db.execute(select(model.like_count).where(model.id == ident))).scalar_one()


async def test_purge_adjusts_like_counts_to_match_remaining_rows(db_session: AsyncSession):
    author = await _mk_user(db_session, "purge-author@example.com", "퍼지작성자")
    leaver = await _mk_user(db_session, "purge-leaver@example.com", "떠날유저", withdrawn_days=40)
    stayer = await _mk_user(db_session, "purge-stayer@example.com", "남을유저")

    now = utc_now()
    post_id = await _mk_post(db_session, author, "퍼지 대상 글", like_count=2)
    comment = Comment(
        post_id=post_id,
        author_id=author,
        content="댓글",
        like_count=2,
        created_at=now,
        updated_at=now,
    )
    db_session.add(comment)
    await db_session.flush()
    comment_id = comment.id

    # 떠날 유저와 남을 유저가 각각 좋아요 → 카운트 2
    db_session.add_all(
        [
            PostLike(post_id=post_id, user_id=leaver, created_at=now),
            PostLike(post_id=post_id, user_id=stayer, created_at=now),
            CommentLike(comment_id=comment_id, user_id=leaver, created_at=now),
            CommentLike(comment_id=comment_id, user_id=stayer, created_at=now),
        ]
    )
    await db_session.commit()

    assert await UserService.purge_withdrawn_users(older_than_days=30, db=db_session) >= 1

    # 떠난 유저의 좋아요 행만 사라지고, 카운트가 남은 행 수와 정확히 일치해야 한다.
    assert await _count(db_session, PostLike, PostLike.post_id == post_id) == 1
    assert await _count(db_session, CommentLike, CommentLike.comment_id == comment_id) == 1
    post_count = await _like_count(db_session, Post, post_id)
    comment_count = await _like_count(db_session, Comment, comment_id)
    assert post_count == 1, f"게시글 like_count 드리프트: {post_count} != 1"
    assert comment_count == 1, f"댓글 like_count 드리프트: {comment_count} != 1"

    # 남을 유저의 좋아요는 보존된다.
    remaining = (
        await db_session.execute(select(PostLike.user_id).where(PostLike.post_id == post_id))
    ).scalar_one()
    assert remaining == stayer


async def test_purge_leaves_unrelated_counts_alone(db_session: AsyncSession):
    """퍼지 대상이 좋아요하지 않은 글의 카운트는 건드리지 않는다."""
    author = await _mk_user(db_session, "purge-other@example.com", "무관작성자")
    await _mk_user(db_session, "purge-leaver2@example.com", "떠날유저2", withdrawn_days=40)
    fan = await _mk_user(db_session, "purge-fan@example.com", "팬유저")

    post_id = await _mk_post(db_session, author, "무관 글", like_count=1)
    db_session.add(PostLike(post_id=post_id, user_id=fan, created_at=utc_now()))
    await db_session.commit()

    await UserService.purge_withdrawn_users(older_than_days=30, db=db_session)

    assert await _like_count(db_session, Post, post_id) == 1


async def test_purge_skips_recently_withdrawn(db_session: AsyncSession):
    """보관 기간이 지나지 않은 탈퇴 유저는 삭제되지 않는다(카운트도 그대로)."""
    author = await _mk_user(db_session, "purge-recent-a@example.com", "최근작성자")
    recent = await _mk_user(db_session, "purge-recent@example.com", "최근탈퇴", withdrawn_days=3)

    post_id = await _mk_post(db_session, author, "최근 글", like_count=1)
    db_session.add(PostLike(post_id=post_id, user_id=recent, created_at=utc_now()))
    await db_session.commit()

    await UserService.purge_withdrawn_users(older_than_days=30, db=db_session)

    still_there = (
        await db_session.execute(select(User.id).where(User.id == recent))
    ).scalar_one_or_none()
    assert still_there == recent
    assert await _like_count(db_session, Post, post_id) == 1
