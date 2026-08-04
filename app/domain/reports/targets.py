# 신고·모더레이션의 POST/COMMENT 정적 배선(likes의 _Target 동형).
# posts·comments가 쌍으로 유지하는 모더레이션 메서드를 타깃 단위로 묶어
# ReportService·AdminService의 타깃 분기를 없앤다. reports가 소유하고 admin이
# 재사용한다 — admin→reports는 기존 엣지 방향이고, posts/comments는 reports를
# import하지 않아 순환이 없다.

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import TargetType
from app.common.exceptions import (
    BaseProjectException,
    CommentNotFoundException,
    PostNotFoundException,
)
from app.domain.comments.repository import CommentsRepository
from app.domain.posts.repository import PostsRepository


class _ModerationRepo(Protocol):
    """posts·comments 저장소가 같은 이름으로 유지하는 모더레이션 계약.

    첫 인자는 positional-only — 구현의 post_id/comment_id 이름 차이를 흡수한다.
    """

    async def increment_report_count(self, target_id: UUID, /, db: AsyncSession) -> int | None: ...

    async def set_blinded(self, target_id: UUID, /, db: AsyncSession) -> bool: ...

    async def unblind(self, target_id: UUID, /, db: AsyncSession) -> bool: ...

    async def reset_reports(self, target_id: UUID, /, db: AsyncSession) -> bool: ...


@dataclass(frozen=True, slots=True)
class ModerationTarget:
    target_type: TargetType
    repo: _ModerationRepo
    not_found: type[BaseProjectException]


_BY_TYPE: dict[TargetType, ModerationTarget] = {
    TargetType.POST: ModerationTarget(
        target_type=TargetType.POST,
        repo=PostsRepository,
        not_found=PostNotFoundException,
    ),
    TargetType.COMMENT: ModerationTarget(
        target_type=TargetType.COMMENT,
        repo=CommentsRepository,
        not_found=CommentNotFoundException,
    ),
}


def moderation_target(value: TargetType | str) -> ModerationTarget:
    # BaseSchema(use_enum_values=True)를 거친 값은 str로 들어온다 — 여기서 한 번만 정규화.
    return _BY_TYPE[TargetType(value)]
