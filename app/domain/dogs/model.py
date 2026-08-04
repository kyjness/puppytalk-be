# 강아지 도메인 ORM(DogProfile)과 쿼리 클래스. 대표 강아지 설정은 한 트랜잭션 내 원자적 처리.
# User 참조는 문자열 관계("User")만 사용 — users.model을 런타임 임포트하지 않는다(순환 차단).

from datetime import date as DateType
from datetime import datetime as DateTimeType
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    bindparam,
    case,
    delete,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, joinedload, mapped_column, relationship

from app.core.ids import new_uuid7
from app.db.base_class import PG_UUID, Base, utc_now
from app.domain.media.model import Image
from app.infra.storage import build_url

if TYPE_CHECKING:
    from app.domain.users.model import User


class DogProfile(Base):
    __tablename__ = "dog_profiles"
    __table_args__ = (
        # 소유자당 대표견 1마리 불변식을 DB로 승격. 명령형 set_representative뿐 아니라
        # 어떤 경로로도 대표견이 2개가 될 수 없게 강제하고, User.representative_dog의
        # uselist=False를 정당화한다.
        Index(
            "uq_dog_profiles_owner_representative",
            "owner_id",
            unique=True,
            postgresql_where=text("is_representative"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID, primary_key=True, default=new_uuid7)
    owner_id: Mapped[UUID] = mapped_column(
        PG_UUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    breed: Mapped[str] = mapped_column(String(100), nullable=False)
    gender: Mapped[str] = mapped_column(String(20), nullable=False)
    birth_date: Mapped[DateType] = mapped_column(Date, nullable=False)
    profile_image_id: Mapped[UUID | None] = mapped_column(
        PG_UUID, ForeignKey("images.id", ondelete="SET NULL"), nullable=True, index=True
    )
    is_representative: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[DateTimeType] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[DateTimeType] = mapped_column(DateTime(timezone=True), nullable=False)

    owner: Mapped["User"] = relationship(
        "User", back_populates="dogs", foreign_keys=[owner_id], lazy="raise_on_sql"
    )
    profile_image: Mapped[Image | None] = relationship(
        "Image", foreign_keys=[profile_image_id], lazy="raise_on_sql"
    )

    @property
    def profile_image_url(self) -> str | None:
        if self.profile_image:
            return build_url(self.profile_image.file_key)
        return None


class DogProfilesRepository:
    @classmethod
    async def get_by_id(cls, dog_id: UUID, owner_id: UUID, db: AsyncSession) -> DogProfile | None:
        stmt = (
            select(DogProfile)
            .where(DogProfile.id == dog_id, DogProfile.owner_id == owner_id)
            .options(joinedload(DogProfile.profile_image))
        )
        result = await db.execute(stmt)
        return result.scalars().one_or_none()

    @classmethod
    async def get_ids_by_owner_id(cls, owner_id: UUID, db: AsyncSession) -> set[UUID]:
        result = await db.execute(select(DogProfile.id).where(DogProfile.owner_id == owner_id))
        return set(result.scalars().all())

    @classmethod
    async def get_owned_ids_in(
        cls, owner_id: UUID, dog_ids: list[UUID], db: AsyncSession
    ) -> set[UUID]:
        if not dog_ids:
            return set()
        result = await db.execute(
            select(DogProfile.id).where(
                DogProfile.owner_id == owner_id,
                DogProfile.id.in_(dog_ids),
            )
        )
        return set(result.scalars().all())

    @classmethod
    async def create_many(
        cls,
        owner_id: UUID,
        rows: list[dict[str, object]],
        *,
        db: AsyncSession,
    ) -> list[DogProfile]:
        if not rows:
            return []
        now = utc_now()
        dogs = [
            DogProfile(
                owner_id=owner_id,
                name=str(r["name"]),
                breed=str(r["breed"]),
                gender=str(r["gender"]),
                birth_date=r["birth_date"],
                profile_image_id=r.get("profile_image_id"),
                is_representative=bool(r.get("is_representative", False)),
                created_at=now,
                updated_at=now,
            )
            for r in rows
        ]
        db.add_all(dogs)
        await db.flush()
        return dogs

    @classmethod
    async def bulk_update_by_owner(
        cls,
        owner_id: UUID,
        rows: list[dict[str, object]],
        *,
        db: AsyncSession,
    ) -> int:
        if not rows:
            return 0
        stmt = (
            update(DogProfile)
            .where(
                DogProfile.owner_id == owner_id,
                DogProfile.id == bindparam("dog_id"),
            )
            .values(
                name=bindparam("name"),
                breed=bindparam("breed"),
                gender=bindparam("gender"),
                birth_date=bindparam("birth_date"),
                is_representative=bindparam("is_representative"),
                profile_image_id=case(
                    (bindparam("touch_profile_image"), bindparam("profile_image_id")),
                    else_=DogProfile.profile_image_id,
                ),
                updated_at=bindparam("updated_at"),
            )
            .returning(DogProfile.id)
        )
        result = await db.execute(stmt, rows)
        return len(list(result.scalars().all()))

    @classmethod
    async def bulk_delete_by_owner_ids(
        cls, owner_id: UUID, dog_ids: list[UUID], db: AsyncSession
    ) -> int:
        if not dog_ids:
            return 0
        result = await db.execute(
            delete(DogProfile)
            .where(DogProfile.owner_id == owner_id, DogProfile.id.in_(dog_ids))
            .returning(DogProfile.id)
        )
        return len(list(result.scalars().all()))

    @classmethod
    async def set_representative(cls, owner_id: UUID, dog_id: UUID, db: AsyncSession) -> bool:
        """해당 유저의 대표 강아지를 dog_id로 설정. 기존 대표 해제 후 설정을 한 트랜잭션 내 원자적으로 수행."""
        await db.execute(
            update(DogProfile)
            .where(DogProfile.owner_id == owner_id)
            .values(is_representative=False)
        )
        r = await db.execute(
            update(DogProfile)
            .where(DogProfile.id == dog_id, DogProfile.owner_id == owner_id)
            .values(is_representative=True, updated_at=utc_now())
            .returning(DogProfile.id)
        )
        return r.scalar_one_or_none() is not None
