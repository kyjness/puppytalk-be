"""이미지 스위퍼 인덱스 — images(id) WHERE deleted_at IS NOT NULL, users(profile_image_id)

Revision ID: 016_image_sweep_indexes
Revises: 015_image_soft_delete
Create Date: 2026-08-19 10:05:00.000000

1. `idx_images_reclaimable` — `WHERE deleted_at IS NOT NULL` 부분 인덱스. 색인 키를 `id`로
   두는 건 스위퍼가 `ORDER BY id LIMIT n` keyset으로 훑기 때문이다. 조건절이 소수만 남기므로
   인덱스 자체가 작고, 스캔량이 `images` 전체가 아니라 **수거 대상 수**에 비례한다.

   이 인덱스가 쓰이려면 스위퍼 쿼리를 둘로 나눠야 한다. 조건을
   `created_at < X OR deleted_at IS NOT NULL`로 합치면 `created_at`엔 인덱스가 없어 Postgres가
   BitmapOr를 못 만들고 전체 스캔으로 떨어진다 — 인덱스가 있어도 안 쓰인다.
   20만 행 · 수거 대상 200건으로 측정: 합본은 Parallel Seq Scan으로 199,800행을 걸러내며
   13.7ms, 분리 후는 Bitmap Index Scan으로 0.4ms.
2. `ix_users_profile_image_id` — `dog_profiles`·`post_images`는 처음부터 있었는데 여기만
   빠져 있었다. 소프트 삭제의 참조 해제와 스위퍼의 `NOT EXISTS` anti-join이 둘 다 이
   컬럼으로 `users`를 찾으므로, 없으면 매번 전체 스캔이다.

둘 다 ADR 0015 규약 — CONCURRENTLY이며 Alembic이 감싸는 트랜잭션을 autocommit_block으로
벗어난다(빠뜨리면 `cannot run inside a transaction block`으로 배포가 깨진다). 인덱스 전용
리비전이라 컬럼 추가(015)와 섞지 않았다 — 부분 적용돼도 `if_not_exists`로 재실행이 안전하다.
ORM 선언(`Image.__table_args__`, `User.profile_image_id index=True`)과 이름·조건이 같다.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "016_image_sweep_indexes"
down_revision: str | None = "015_image_soft_delete"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RECLAIMABLE_INDEX = "idx_images_reclaimable"
_RECLAIMABLE_WHERE = "deleted_at IS NOT NULL"
_USERS_IMAGE_INDEX = "ix_users_profile_image_id"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            _RECLAIMABLE_INDEX,
            "images",
            ["id"],
            unique=False,
            postgresql_concurrently=True,
            postgresql_where=sa.text(_RECLAIMABLE_WHERE),
            if_not_exists=True,
        )
        op.create_index(
            _USERS_IMAGE_INDEX,
            "users",
            ["profile_image_id"],
            unique=False,
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            _USERS_IMAGE_INDEX,
            table_name="users",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.drop_index(
            _RECLAIMABLE_INDEX,
            table_name="images",
            postgresql_concurrently=True,
            if_exists=True,
        )
