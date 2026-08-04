# 사용자 도메인 ORM(User·UserBlock)과 쿼리 클래스. 프로필 이미지는 profile_image_id(FK).
# DogProfile·Report ORM은 각자의 도메인(dogs·reports) model.py 소유 — User.dogs 관계가
# DogProfile 컬럼을 직접 참조하므로 여기서 런타임 임포트한다(역방향 의존 없음 → 순환 없음).
from datetime import datetime as DateTimeType
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    and_,
    delete,
    exists,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, joinedload, mapped_column, relationship, selectinload

from app.common.enums import UserStatus
from app.core.ids import new_uuid7
from app.db.base_class import PG_UUID, Base, utc_now
from app.domain.dogs.model import DogProfile
from app.domain.media.model import Image
from app.infra.storage import build_url


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(PG_UUID, primary_key=True, default=new_uuid7)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    __mapper_args__ = {"version_id_col": version}
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    nickname: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    profile_image_id: Mapped[UUID | None] = mapped_column(
        PG_UUID, ForeignKey("images.id", ondelete="SET NULL"), nullable=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="USER")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=UserStatus.ACTIVE.value)
    created_at: Mapped[DateTimeType] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[DateTimeType] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[DateTimeType | None] = mapped_column(DateTime(timezone=True), nullable=True)

    profile_image: Mapped[Image | None] = relationship(
        "Image", foreign_keys=[profile_image_id], lazy="raise_on_sql"
    )
    dogs: Mapped[list[DogProfile]] = relationship(
        "DogProfile",
        back_populates="owner",
        foreign_keys=[DogProfile.owner_id],
        order_by="DogProfile.id",
        lazy="raise_on_sql",
    )
    # 대표견 전용 뷰 관계. dogs를 .and_() 필터로 로드하면 컬렉션 자체가 대표견 1마리로
    # 잘려 세션에 캐시되는 트랩이 있어, 대표견은 dogs를 건드리지 않는 별도 관계로 로드한다.
    # viewonly라 영속·overlaps 검사에서 제외되고, '소유자당 대표견 1마리'는 부분 유니크
    # 인덱스(uq_dog_profiles_owner_representative)가 보장하므로 uselist=False가 정당하다.
    representative_dog: Mapped[DogProfile | None] = relationship(
        "DogProfile",
        primaryjoin="and_(DogProfile.owner_id == User.id, DogProfile.is_representative == True)",
        foreign_keys=[DogProfile.owner_id],
        uselist=False,
        viewonly=True,
        lazy="raise_on_sql",
    )

    @property
    def profile_image_url(self) -> str | None:
        if self.profile_image:
            return build_url(self.profile_image.file_key)
        return None

    @property
    def is_active(self) -> bool:
        # 하위호환: 레거시 코드에서 user.is_active를 계속 사용할 수 있게 유지
        return UserStatus.is_active_value(self.status)


class UserBlock(Base):
    __tablename__ = "user_blocks"
    # 복합 PK(blocker_id, blocked_id)가 유니크를 보장하므로 별도 UniqueConstraint는 두지 않는다.

    blocker_id: Mapped[UUID] = mapped_column(
        PG_UUID, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    blocked_id: Mapped[UUID] = mapped_column(
        PG_UUID, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[DateTimeType] = mapped_column(DateTime(timezone=True), nullable=False)

    blocker: Mapped["User"] = relationship("User", foreign_keys=[blocker_id], lazy="raise_on_sql")
    blocked: Mapped["User"] = relationship("User", foreign_keys=[blocked_id], lazy="raise_on_sql")


def author_not_blocked_clause(author_col: Any, blocker_id: UUID | None) -> Any | None:
    """blocker가 차단한 작성자를 제외하는 조건(대상 없으면 None).

    "차단한 저자의 콘텐츠를 숨긴다"는 users 도메인 정책의 단일 정의처 —
    posts·comments가 동일 술어를 공유한다(정책 변경 시 여기 한 곳만).
    """
    if blocker_id is None:
        return None
    return ~exists(1).where(
        UserBlock.blocker_id == blocker_id,
        UserBlock.blocked_id == author_col,
    )


def author_display_loads(author_rel: Any) -> Any:
    """작성자 표시용 공통 eager load(프로필 이미지 + 대표견 1마리).

    응답은 author.representative_dog 하나만 쓰므로 대표견 전용 뷰 관계로 로드한다.
    dogs를 .and_() 필터로 로드하면 컬렉션 자체가 잘려 세션에 캐시되는 트랩이 있어
    dogs는 건드리지 않는다. posts(Post.user)·comments(Comment.author)가 공유한다.
    """
    return joinedload(author_rel).options(
        joinedload(User.profile_image),
        selectinload(User.representative_dog).joinedload(DogProfile.profile_image),
    )


class UsersRepository:
    @classmethod
    async def create_user(
        cls,
        email: str,
        hashed_password: str,
        nickname: str,
        profile_image_id: UUID | None = None,
        *,
        db: AsyncSession,
    ) -> User:
        now = utc_now()
        user = User(
            email=email.lower(),
            password=hashed_password,
            nickname=nickname,
            profile_image_id=profile_image_id,
            status=UserStatus.ACTIVE.value,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        db.add(user)
        await db.flush()
        return user

    @classmethod
    async def get_user_by_id(cls, user_id: UUID, db: AsyncSession) -> User | None:
        stmt = (
            select(User)
            .where(User.id == user_id, User.deleted_at.is_(None))
            .options(joinedload(User.profile_image))
        )
        result = await db.execute(stmt)
        return result.unique().scalars().one_or_none()

    @classmethod
    async def get_user_by_id_including_deleted(cls, user_id: UUID, db: AsyncSession) -> User | None:
        stmt = select(User).where(User.id == user_id).options(joinedload(User.profile_image))
        result = await db.execute(stmt)
        return result.unique().scalars().one_or_none()

    @classmethod
    async def get_user_by_id_with_dogs(cls, user_id: UUID, db: AsyncSession) -> User | None:
        stmt = (
            select(User)
            .where(User.id == user_id, User.deleted_at.is_(None))
            .options(
                joinedload(User.profile_image),
                selectinload(User.dogs).joinedload(DogProfile.profile_image),
                selectinload(User.representative_dog).joinedload(DogProfile.profile_image),
            )
        )
        result = await db.execute(stmt)
        return result.unique().scalars().one_or_none()

    @classmethod
    async def get_user_by_email(cls, email: str, db: AsyncSession) -> User | None:
        stmt = (
            select(User)
            .where(User.email == email.lower(), User.deleted_at.is_(None))
            .options(joinedload(User.profile_image))
        )
        result = await db.execute(stmt)
        return result.unique().scalars().one_or_none()

    @classmethod
    async def get_password_hash(cls, user_id: UUID, db: AsyncSession) -> str | None:
        result = await db.execute(
            select(User.password).where(User.id == user_id, User.deleted_at.is_(None))
        )
        return result.scalar_one_or_none()

    @classmethod
    async def email_exists(cls, email: str, db: AsyncSession) -> bool:
        result = await db.execute(
            select(User.id).where(User.email == email.lower(), User.deleted_at.is_(None)).limit(1)
        )
        return result.first() is not None

    @classmethod
    async def nickname_exists(cls, nickname: str, db: AsyncSession) -> bool:
        result = await db.execute(
            select(User.id).where(User.nickname == nickname, User.deleted_at.is_(None)).limit(1)
        )
        return result.first() is not None

    _UPDATE_USER_ALLOWED = frozenset({"nickname", "profile_image_id", "status"})

    @classmethod
    async def update_user(
        cls,
        user_id: UUID,
        *,
        db: AsyncSession,
        **fields: Any,
    ) -> bool:
        allowed = {k: v for k, v in fields.items() if k in cls._UPDATE_USER_ALLOWED}
        if not allowed:
            return True
        allowed["updated_at"] = utc_now()
        r = await db.execute(
            update(User)
            .where(User.id == user_id, User.deleted_at.is_(None))
            .values(**allowed)
            .returning(User.id)
        )
        return r.scalar_one_or_none() is not None

    @classmethod
    async def update_password(cls, user_id: UUID, hashed_password: str, db: AsyncSession) -> bool:
        r = await db.execute(
            update(User)
            .where(User.id == user_id, User.deleted_at.is_(None))
            .values(password=hashed_password)
            .returning(User.id)
        )
        return r.scalar_one_or_none() is not None

    @classmethod
    async def get_blocked_users(
        cls,
        blocker_id: UUID,
        *,
        db: AsyncSession,
        size: int,
        cursor: UUID | None = None,
    ) -> list[User]:
        """내가 차단한 유저 목록을 keyset로 조회한다(size+1건으로 has_more 판정).

        정렬·커서 축이 blocked_id인 이유: user_blocks의 PK가 (blocker_id, blocked_id)라
        blocker_id 필터 + blocked_id 정렬이 **추가 인덱스 없이 PK로 완전히 커버**된다.
        차단 시점(created_at) 정렬은 복합 커서 인코딩이 필요한데, 이 목록에 그 복잡도는
        정당화되지 않는다(ADR 0002의 cursor 표준은 그대로 따른다).
        """
        stmt = (
            select(User)
            .join(UserBlock, User.id == UserBlock.blocked_id)
            .where(
                UserBlock.blocker_id == blocker_id,
                User.deleted_at.is_(None),
            )
            .options(joinedload(User.profile_image))
        )
        if cursor is not None:
            stmt = stmt.where(UserBlock.blocked_id < cursor)
        stmt = stmt.order_by(UserBlock.blocked_id.desc()).limit(size + 1)
        result = await db.execute(stmt)
        return list(result.unique().scalars().all())

    @classmethod
    async def block_exists(cls, blocker_id: UUID, blocked_id: UUID, db: AsyncSession) -> bool:
        result = await db.execute(
            select(UserBlock.blocker_id)
            .where(
                UserBlock.blocker_id == blocker_id,
                UserBlock.blocked_id == blocked_id,
            )
            .limit(1)
        )
        return result.first() is not None

    @classmethod
    async def get_status_and_block_between(
        cls, user_id: UUID, other_id: UUID, db: AsyncSession
    ) -> Any | None:
        """상대(other)의 status와 양방향 차단 여부(방향 무관)를 한 문장으로.

        DM 등 상호작용 진입점의 사전 검사용 — 존재·활성 확인과 차단 판정에 각각
        왕복하지 않는다. 반환 행은 (status, blocked); 미존재·탈퇴면 None.
        """
        blocked = (
            exists(1)
            .where(
                or_(
                    and_(UserBlock.blocker_id == user_id, UserBlock.blocked_id == other_id),
                    and_(UserBlock.blocker_id == other_id, UserBlock.blocked_id == user_id),
                )
            )
            .label("blocked")
        )
        result = await db.execute(
            select(User.status, blocked).where(User.id == other_id, User.deleted_at.is_(None))
        )
        return result.one_or_none()

    @classmethod
    async def block_user(cls, blocker_id: UUID, blocked_id: UUID, db: AsyncSession) -> None:
        db.add(
            UserBlock(
                blocker_id=blocker_id,
                blocked_id=blocked_id,
                created_at=utc_now(),
            )
        )
        await db.flush()

    @classmethod
    async def unblock_user(cls, blocker_id: UUID, blocked_id: UUID, db: AsyncSession) -> int:
        r = await db.execute(
            delete(UserBlock)
            .where(
                UserBlock.blocker_id == blocker_id,
                UserBlock.blocked_id == blocked_id,
            )
            .returning(UserBlock.blocker_id)
        )
        return len(list(r.scalars().all()))

    _DELETED_AT_MAX_LEN = 255

    @classmethod
    def _deleted_at_suffix(cls, user_id: UUID) -> str:
        ts = int(utc_now().timestamp())
        return f"_deleted_{user_id}_{ts}"

    @classmethod
    def _mask_for_withdrawal(cls, value: str, suffix: str) -> str:
        max_len = cls._DELETED_AT_MAX_LEN
        prefix_len = max(0, max_len - len(suffix))
        base = str(value)[:prefix_len] if value else ""
        return (base + suffix)[:max_len]

    @classmethod
    async def delete_user(cls, user_id: UUID, db: AsyncSession) -> bool:
        stmt = select(User.id, User.email, User.nickname).where(
            User.id == user_id, User.deleted_at.is_(None)
        )
        result = await db.execute(stmt)
        row = result.one_or_none()
        if not row:
            return False
        now = utc_now()
        suffix = cls._deleted_at_suffix(user_id)
        new_email = cls._mask_for_withdrawal(row.email, suffix)
        new_nickname = cls._mask_for_withdrawal(row.nickname, suffix)
        r = await db.execute(
            update(User)
            .where(User.id == user_id, User.deleted_at.is_(None))
            .values(
                email=new_email,
                nickname=new_nickname,
                status=UserStatus.WITHDRAWN.value,
                profile_image_id=None,
                deleted_at=now,
                updated_at=now,
            )
            .returning(User.id)
        )
        return r.scalar_one_or_none() is not None

    @classmethod
    async def purge_withdrawn_users_older_than(
        cls,
        *,
        older_than_days: int,
        limit: int,
        db: AsyncSession,
    ) -> list[UUID]:
        """탈퇴(WITHDRAWN) + deleted_at 기준 N일 경과 유저를 하드 삭제.

        - 대량 삭제로 인한 락을 줄이기 위해 limit 단위로 청크 처리한다.
        - FK ondelete(CASCADE/SET NULL)에 의존해 연관 데이터 정합성 유지.
        """
        cutoff = utc_now() - timedelta(days=older_than_days)
        id_stmt = (
            select(User.id)
            .where(
                User.status == UserStatus.WITHDRAWN.value,
                User.deleted_at.is_not(None),
                User.deleted_at < cutoff,
            )
            .limit(int(limit))
        )
        result = await db.execute(delete(User).where(User.id.in_(id_stmt)).returning(User.id))
        await db.flush()
        return list(result.scalars().all())
