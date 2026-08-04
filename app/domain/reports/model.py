# 신고 도메인 ORM(Report)과 쿼리 클래스.
# User 참조는 문자열 관계("User")만 사용 — users.model을 런타임 임포트하지 않는다(순환 차단).

from collections import defaultdict
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, and_, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.common.enums import TargetType
from app.core.ids import new_uuid7
from app.db.base_class import PG_UUID, Base, utc_now

if TYPE_CHECKING:
    from app.domain.users.model import User


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (
        # 관리자 신고 집계·delete_by_target는 모두 WHERE target_type AND target_id (AND deleted_at
        # IS NULL)로 조회한다. 모든 read 경로가 미삭제만 보므로 부분 인덱스로 살아있는 신고만 커버.
        Index(
            "ix_reports_target",
            "target_type",
            "target_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID, primary_key=True, default=new_uuid7)
    reporter_id: Mapped[UUID] = mapped_column(
        PG_UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_id: Mapped[UUID] = mapped_column(PG_UUID, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    reporter: Mapped["User"] = relationship("User", foreign_keys=[reporter_id], lazy="raise_on_sql")


def _not_deleted():
    return Report.deleted_at.is_(None)


class ReportsRepository:
    @classmethod
    async def create_report(
        cls,
        reporter_id: UUID,
        target_type: TargetType,
        target_id: UUID,
        reason: str | None,
        db: AsyncSession,
    ) -> None:
        # TargetType은 StrEnum — str 컬럼에 값 그대로 바인딩된다.
        db.add(
            Report(
                reporter_id=reporter_id,
                target_type=target_type,
                target_id=target_id,
                reason=reason,
                created_at=utc_now(),
            )
        )
        await db.flush()

    @classmethod
    async def bulk_report_meta(
        cls,
        *,
        post_ids: list[UUID],
        comment_ids: list[UUID],
        db: AsyncSession,
    ) -> tuple[dict[tuple[TargetType, UUID], list[str]], dict[tuple[TargetType, UUID], datetime]]:
        """(target_type, target_id)별 reason 목록(created_at 오름차순, 빈 reason 제외)과
        마지막 신고 시각(빈 reason 포함 전체 기준)을 한 번에 반환.

        reason 행이 created_at을 이미 실어 오므로 max(created_at)는 같은 순회에서 파생 —
        별도 max 쿼리가 필요 없다. 타깃 유형을 OR-arm으로 나눠 부분 인덱스
        (target_type, target_id)의 선두 컬럼을 살린다. 유일 호출자(관리자 목록)의
        페이지 상한이 100이라 IN 청크 분할은 두지 않는다.
        """
        reasons: defaultdict[tuple[TargetType, UUID], list[str]] = defaultdict(list)
        last_at: dict[tuple[TargetType, UUID], datetime] = {}
        arms = [
            and_(Report.target_type == ttype, Report.target_id.in_(ids))
            for ttype, ids in ((TargetType.POST, post_ids), (TargetType.COMMENT, comment_ids))
            if ids
        ]
        if not arms:
            return {}, {}
        stmt = (
            select(Report.target_type, Report.target_id, Report.reason, Report.created_at)
            .where(or_(*arms), _not_deleted())
            .order_by(Report.target_id, Report.created_at.asc())
        )
        result = await db.execute(stmt)
        for ttype_raw, tid, reason, created_at in result.all():
            key = (TargetType(ttype_raw), tid)
            if reason:
                reasons[key].append(reason)
            prev = last_at.get(key)
            if prev is None or created_at > prev:
                last_at[key] = created_at
        return dict(reasons), last_at

    @classmethod
    async def delete_by_target(
        cls, target_type: TargetType, target_id: UUID, db: AsyncSession
    ) -> None:
        await db.execute(
            update(Report)
            .where(
                Report.target_type == target_type,
                Report.target_id == target_id,
                _not_deleted(),
            )
            .values(deleted_at=utc_now())
        )
