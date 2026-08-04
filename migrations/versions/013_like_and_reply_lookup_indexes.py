"""좋아요 소유자·대댓글 조회 인덱스 추가

Revision ID: 013_like_reply_lookup_indexes
Revises: 012_drop_redundant_user_block_unique
Create Date: 2026-08-04 20:10:00.000000

세 인덱스 모두 "선행 컬럼이 아니라 못 쓰던 조회"를 덮는다.

1) post_likes(user_id) / comment_likes(user_id)
   PK가 (post_id, user_id)·(comment_id, user_id)라 user_id는 선행 컬럼이 아니다. 탈퇴 유저
   퍼지가 `WHERE user_id IN (...)`로 집계해 like_count를 보정하는데(청크마다 반복) 매번
   테이블 전체를 훑는다. 유저 삭제 시 ON DELETE CASCADE도 같은 이유로 풀스캔이다.

2) comments(parent_id, id DESC) WHERE 미삭제·미블라인드
   대댓글 preview(window function)와 "더보기" keyset이 부모별로 정렬한다. 단일 컬럼
   ix_comments_parent_id만으로는 파티션 전체를 읽고 정렬해야 한다. 부분 인덱스 술어는
   _reply_visible_conditions와 일치시켜 인덱스만으로 가시성까지 거른다.

ADR 0015 규약: 운영 데이터가 쌓인 테이블이므로 CONCURRENTLY. Alembic이 감싸는 트랜잭션을
autocommit_block으로 벗어나야 한다 — 빠뜨리면 배포가 깨진다.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "013_like_reply_lookup_indexes"
down_revision: str | None = "012_drop_redundant_user_block_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REPLY_VISIBLE = "deleted_at IS NULL AND is_blinded IS FALSE"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_post_likes_user_id",
            "post_likes",
            ["user_id"],
            unique=False,
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        op.create_index(
            "ix_comment_likes_user_id",
            "comment_likes",
            ["user_id"],
            unique=False,
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        op.create_index(
            "idx_comments_parent_visible",
            "comments",
            ["parent_id", "id"],
            unique=False,
            postgresql_concurrently=True,
            postgresql_where=sa.text(_REPLY_VISIBLE),
            if_not_exists=True,
        )


def downgrade() -> None:
    # 롤백이 쓰기를 멈추면 롤백이 2차 장애가 된다 — 내릴 때도 CONCURRENTLY.
    with op.get_context().autocommit_block():
        op.drop_index(
            "idx_comments_parent_visible", table_name="comments", postgresql_concurrently=True
        )
        op.drop_index(
            "ix_comment_likes_user_id", table_name="comment_likes", postgresql_concurrently=True
        )
        op.drop_index(
            "ix_post_likes_user_id", table_name="post_likes", postgresql_concurrently=True
        )
