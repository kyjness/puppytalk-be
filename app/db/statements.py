# 단일 행 UPDATE…RETURNING 공용 실행기. posts·comments의 카운터·모더레이션 계열이
# 공유하는 도메인 무관 메커니즘 — 가드(deleted_at)·updated_at 갱신 여부 같은 시맨틱은
# 각 도메인 리포지토리의 얇은 래퍼가 결정한다.

from typing import Any

from sqlalchemy import update
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
