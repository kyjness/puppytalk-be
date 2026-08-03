# 신고 접수·report_count 증가·임계값 도달 시 자동 블라인드. 단일 트랜잭션.
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import TargetType
from app.common.exceptions import CommentNotFoundException, PostNotFoundException
from app.core.config import settings
from app.domain.comments.repository import CommentsModel
from app.domain.posts.repository import PostsModel
from app.domain.reports.model import ReportsModel
from app.domain.reports.schema import ReportCreateRequest, ReportSubmitData


class ReportService:
    @classmethod
    async def _create_report_and_maybe_blind(
        cls,
        reporter_id: UUID,
        target_type: TargetType,
        target_id: UUID,
        reason: str | None,
        db: AsyncSession,
    ) -> bool:
        await ReportsModel.create_report(reporter_id, target_type, target_id, reason, db=db)
        if target_type == TargetType.POST:
            new_count = await PostsModel.increment_report_count(target_id, db=db)
        else:
            new_count = await CommentsModel.increment_report_count(target_id, db=db)
        blinded = False
        if new_count is not None and new_count >= settings.REPORT_BLIND_THRESHOLD:
            if target_type == TargetType.POST:
                await PostsModel.set_blinded(target_id, db=db)
            else:
                await CommentsModel.set_blinded(target_id, db=db)
            blinded = True
        return blinded

    @classmethod
    async def submit_report(
        cls,
        reporter_id: UUID,
        data: ReportCreateRequest,
        db: AsyncSession,
    ) -> ReportSubmitData:
        async with db.begin():
            if data.target_type == TargetType.POST:
                if await PostsModel.get_post_author_id(data.target_id, db=db) is None:
                    raise PostNotFoundException()
            else:
                meta = await CommentsModel.get_comment_meta(data.target_id, db=db)
                if meta is None or meta.deleted_at is not None:
                    raise CommentNotFoundException()

            # Pydantic/Config(use_enum_values 등) 조합에 따라 reason이 Enum이 아니라 str로 들어올 수 있음.
            reason_value = getattr(data.reason, "value", data.reason)
            blinded = await cls._create_report_and_maybe_blind(
                reporter_id,
                data.target_type,
                data.target_id,
                reason_value,
                db=db,
            )
            return ReportSubmitData(reported=True, blinded=blinded)
