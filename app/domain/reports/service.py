# 신고 접수·report_count 증가·임계값 도달 시 자동 블라인드. 단일 트랜잭션.
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.domain.reports.model import ReportsRepository
from app.domain.reports.schema import ReportCreateRequest, ReportSubmitData
from app.domain.reports.targets import moderation_target


class ReportService:
    @classmethod
    async def submit_report(
        cls,
        reporter_id: UUID,
        data: ReportCreateRequest,
        db: AsyncSession,
    ) -> ReportSubmitData:
        target = moderation_target(data.target_type)
        async with db.begin():
            # increment의 RETURNING이 존재 확인을 겸한다(None = 미존재·삭제·저자 없는 글) —
            # 별도 사전 조회 쿼리 없음.
            new_count = await target.repo.increment_report_count(data.target_id, db=db)
            if new_count is None:
                raise target.not_found()
            await ReportsRepository.create_report(
                reporter_id, target.target_type, data.target_id, data.reason, db=db
            )
            blinded = False
            if new_count >= settings.REPORT_BLIND_THRESHOLD:
                await target.repo.set_blinded(data.target_id, db=db)
                blinded = True
            return ReportSubmitData(reported=True, blinded=blinded)
