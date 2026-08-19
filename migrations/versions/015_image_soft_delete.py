"""이미지 소프트 삭제 — images.deleted_at

Revision ID: 015_image_soft_delete
Revises: 014_comment_root_sort_indexes
Create Date: 2026-08-19 10:00:00.000000

이미지 삭제가 한 요청 안에서 S3와 DB를 둘 다 변경해, 어느 쪽이 뒤에 실패하든 정합성이
깨졌다(backlog #44). 요청은 `deleted_at`만 찍고 스토리지 삭제는 주기 스위퍼가 맡는다(ADR 0019).

`deleted_at`은 nullable이라 ADD COLUMN이 테이블을 재작성하지 않는다(PG11+ — 기본값이 없으면
카탈로그만 갱신). 라이브에서 안전하다.

스위퍼용 인덱스는 **016**에 따로 둔다 — ADR 0015 규약상 CONCURRENTLY 인덱스 리비전은 트랜잭션
밖에서 돌아 부분 적용이 가능하므로 다른 DDL과 섞지 않는다.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "015_image_soft_delete"
down_revision: str | None = "014_comment_root_sort_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "images",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("images", "deleted_at")
