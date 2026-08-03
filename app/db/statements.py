# 단일 행 DML 공용 실행기. posts·comments의 카운터·모더레이션 계열과 likes의 링크 행
# 조작이 공유하는 도메인 무관 메커니즘 — 가드(deleted_at)·updated_at 갱신 여부 같은
# 시맨틱은 각 도메인 리포지토리의 얇은 래퍼가 결정한다.

from typing import Any, cast

from sqlalchemy import delete, literal, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession


async def update_one_returning(
    db: AsyncSession,
    model: type[Any],
    conds: list[Any],
    values: dict[str, Any],
    returning: Any,
) -> Any | None:
    r = await db.execute(update(model).where(*conds).values(**values).returning(returning))
    return r.scalar_one_or_none()


async def insert_ignore(
    db: AsyncSession,
    model: type[Any],
    values: dict[str, Any],
    index_elements: list[Any],
) -> bool:
    """ON CONFLICT DO NOTHING 삽입. True=신규 삽입, False=이미 존재(충돌 무시).

    판정은 rowcount가 아니라 RETURNING 유무 — SQLAlchemy가 rowcount를 보장하는 건
    UPDATE/DELETE뿐이고, 이 드라이버 조합의 INSERT rowcount는 -1이다(실측).
    RETURNING은 literal(1) — index_elements(충돌 타깃)에 컬럼 역할을 겹쳐 지우지 않는다.
    """
    stmt = (
        pg_insert(model)
        .values(**values)
        .on_conflict_do_nothing(index_elements=index_elements)
        .returning(literal(1))
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def delete_rows(db: AsyncSession, model: type[Any], conds: list[Any]) -> int:
    """조건 일치 행 삭제 후 삭제 행 수 반환 — RETURNING 없이 rowcount로 센다."""
    cr = cast(CursorResult[Any], await db.execute(delete(model).where(*conds)))
    return int(cr.rowcount or 0)
