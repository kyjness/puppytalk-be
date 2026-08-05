"""루트 댓글 정렬 인덱스 추가(인기순·최신순)

Revision ID: 014_comment_root_sort_indexes
Revises: 013_like_reply_lookup_indexes
Create Date: 2026-08-05 11:00:00.000000

댓글 목록이 커서에서 offset+total로 바뀌면서(ADR 0016) 정렬 축이 둘이 됐다:
인기순 `like_count DESC, id DESC`와 최신순 `id DESC`. 지금까지는 ix_comments_post_id
단일 인덱스로 파티션을 읽고 매번 정렬했는데, offset 페이지는 요청마다 정렬을 타므로
순서까지 인덱스가 받아야 한다.

두 인덱스 모두 ASC로 만든다 — post_id 등치 뒤 정렬 축이 전부 DESC라 btree 역방향
스캔으로 그대로 커버된다(방향이 섞일 때만 DESC 선언이 필요하다).

부분 술어는 쿼리의 `parent_id IS NULL AND is_blinded IS FALSE`와 일치시킨다.
deleted_at은 넣지 않는다 — 삭제된 루트도 표시 가능한 대댓글이 있으면 placeholder로
목록에 남기 때문이다.

ADR 0015 규약: 운영 데이터가 쌓인 테이블이므로 CONCURRENTLY. Alembic이 감싸는 트랜잭션을
autocommit_block으로 벗어나야 한다 — 빠뜨리면 배포가 깨진다.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "014_comment_root_sort_indexes"
down_revision: str | None = "013_like_reply_lookup_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROOT_VISIBLE = "parent_id IS NULL AND is_blinded IS FALSE"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "idx_comments_post_popular",
            "comments",
            ["post_id", "like_count", "id"],
            unique=False,
            postgresql_concurrently=True,
            postgresql_where=sa.text(_ROOT_VISIBLE),
            if_not_exists=True,
        )
        op.create_index(
            "idx_comments_post_latest",
            "comments",
            ["post_id", "id"],
            unique=False,
            postgresql_concurrently=True,
            postgresql_where=sa.text(_ROOT_VISIBLE),
            if_not_exists=True,
        )


def downgrade() -> None:
    # 롤백이 쓰기를 멈추면 롤백이 2차 장애가 된다 — 내릴 때도 CONCURRENTLY.
    with op.get_context().autocommit_block():
        op.drop_index(
            "idx_comments_post_latest", table_name="comments", postgresql_concurrently=True
        )
        op.drop_index(
            "idx_comments_post_popular", table_name="comments", postgresql_concurrently=True
        )
