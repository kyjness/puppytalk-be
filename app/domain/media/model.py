from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    column,
    delete,
    exists,
    select,
    table,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.core.ids import new_uuid7
from app.db.base_class import PG_UUID, Base, utc_now


class Image(Base):
    __tablename__ = "images"

    id: Mapped[UUID] = mapped_column(PG_UUID, primary_key=True, default=new_uuid7)
    file_key: Mapped[str] = mapped_column(String(255), nullable=False)
    file_url: Mapped[str] = mapped_column(String(999), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uploader_id: Mapped[UUID | None] = mapped_column(
        PG_UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MediaRepository:
    @classmethod
    async def create_temp_image(
        cls,
        file_key: str,
        file_url: str,
        content_type: str | None,
        size: int | None,
        *,
        db: AsyncSession,
    ) -> Image:
        return await cls.create_image(
            file_key=file_key,
            file_url=file_url,
            content_type=content_type,
            size=size,
            uploader_id=None,
            db=db,
        )

    @classmethod
    async def create_image(
        cls,
        file_key: str,
        file_url: str,
        content_type: str | None = None,
        size: int | None = None,
        uploader_id: UUID | None = None,
        *,
        db: AsyncSession,
    ) -> Image:
        img = Image(
            file_key=file_key,
            file_url=file_url,
            content_type=content_type,
            size=size,
            uploader_id=uploader_id,
            created_at=utc_now(),
        )
        db.add(img)
        await db.flush()
        return img

    @classmethod
    async def get_image_by_id(cls, image_id: UUID, db: AsyncSession) -> Image | None:
        stmt = select(Image).where(Image.id == image_id)
        result = await db.execute(stmt)
        return result.scalars().one_or_none()

    @classmethod
    async def get_images_by_ids(cls, image_ids: list[UUID], db: AsyncSession) -> list[Image]:
        if not image_ids:
            return []
        stmt = select(Image).where(Image.id.in_(image_ids))
        result = await db.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    async def claim_image_ownership(cls, image_id: UUID, user_id: UUID, db: AsyncSession) -> bool:
        r = await db.execute(
            update(Image)
            .where(Image.id == image_id)
            .where(Image.uploader_id.is_(None))
            .values(
                uploader_id=user_id,
            )
            .returning(Image.id)
        )
        ok = r.scalar_one_or_none() is not None
        if ok:
            await db.flush()
        return ok

    @classmethod
    async def delete_image_record(cls, image: Image, db: AsyncSession) -> None:
        await db.delete(image)
        await db.flush()

    @classmethod
    async def delete_images_by_ids(cls, image_ids: list[UUID], db: AsyncSession) -> int:
        if not image_ids:
            return 0
        result = await db.execute(delete(Image).where(Image.id.in_(image_ids)).returning(Image.id))
        await db.flush()
        return len(list(result.scalars().all()))

    @classmethod
    async def get_expired_signup_images(
        cls, db: AsyncSession, *, limit: int | None = None, after_id: UUID | None = None
    ) -> list[Image]:
        cutoff = utc_now() - timedelta(seconds=settings.SIGNUP_IMAGE_TOKEN_TTL_SECONDS)
        stmt = (
            select(Image)
            .where(
                Image.uploader_id.is_(None),
                Image.created_at < cutoff,
            )
            .order_by(Image.id.asc())
        )
        if after_id is not None:
            stmt = stmt.where(Image.id > after_id)
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    async def get_orphan_images_older_than(
        cls,
        *,
        older_than_hours: int,
        db: AsyncSession,
        limit: int | None = None,
        after_id: UUID | None = None,
    ) -> list[Image]:
        cutoff = utc_now() - timedelta(hours=older_than_hours)
        users_t = table("users", column("profile_image_id", PG_UUID))
        dogs_t = table("dog_profiles", column("profile_image_id", PG_UUID))
        post_images_t = table("post_images", column("image_id", PG_UUID))
        stmt = (
            select(Image)
            .where(Image.created_at < cutoff)
            .where(
                ~exists(
                    select(1).select_from(users_t).where(users_t.c.profile_image_id == Image.id)
                )
            )
            .where(
                ~exists(select(1).select_from(dogs_t).where(dogs_t.c.profile_image_id == Image.id))
            )
            .where(
                ~exists(
                    select(1).select_from(post_images_t).where(post_images_t.c.image_id == Image.id)
                )
            )
            .order_by(Image.id.asc())
        )
        if after_id is not None:
            stmt = stmt.where(Image.id > after_id)
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    async def delete_image_if_owned(
        cls,
        image_id: UUID,
        user_id: UUID,
        *,
        db: AsyncSession,
    ) -> bool:
        r = await db.execute(
            delete(Image)
            .where(Image.id == image_id, Image.uploader_id == user_id)
            .returning(Image.id)
        )
        return r.scalar_one_or_none() is not None
