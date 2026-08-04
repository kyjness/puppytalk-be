# 단일 행 DML 공용 실행기. posts·comments의 카운터·모더레이션 계열과 likes의 링크 행
# 조작이 공유하는 도메인 무관 메커니즘 — 가드(deleted_at)·updated_at 갱신 여부 같은
# 시맨틱은 각 도메인 리포지토리의 얇은 래퍼가 결정한다.

from typing import Any, cast

from sqlalchemy import delete, func, literal, select, update
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


async def decrement_counter_by_link_owner(
    db: AsyncSession,
    *,
    target_model: type[Any],
    counter_col: Any,
    link_target_col: Any,
    link_owner_col: Any,
    owner_ids: list[Any],
) -> None:
    """링크 행의 소유자들을 지우기 **직전에**, 그 링크 수만큼 대상의 카운터를 깎는다.

    좋아요처럼 링크 행이 유저에 CASCADE로 매달려 있고 대상(게시글·댓글)은 SET NULL로 살아남는
    구조에서, FK는 행만 정리하고 비정규화 카운터는 모른다 — 보정하지 않으면 카운터가 영구히
    부풀어 있다. 삭제 후에는 어떤 링크가 사라졌는지 알 수 없어 호출 순서가 강제된다.

    음수 방지는 카운터 규약대로 GREATEST(x - n, 0).
    """
    if not owner_ids:
        return
    agg = (
        select(link_target_col.label("target_id"), func.count().label("n"))
        .where(link_owner_col.in_(owner_ids))
        .group_by(link_target_col)
        .subquery()
    )
    await db.execute(
        update(target_model)
        .where(target_model.id == agg.c.target_id)
        .values({counter_col.key: func.greatest(counter_col - agg.c.n, 0)})
    )
