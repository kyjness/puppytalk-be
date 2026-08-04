"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

데이터가 쌓인 테이블에 인덱스를 추가·삭제한다면 CONCURRENTLY 규약을 따른다
(docs/adr/0015-index-migration-concurrently.md):

    with op.get_context().autocommit_block():   # 빠뜨리면 배포가 깨진다
        op.create_index(..., postgresql_concurrently=True, if_not_exists=True)

빈 테이블·새 테이블·시드 테이블은 예외 — 일반 create_index로 둔다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: Union[str, None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
