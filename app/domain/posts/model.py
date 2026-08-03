# 게시글·post_images ORM 정의. 쿼리/트랜잭션 로직은 repository.py.

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.ids import new_uuid7
from app.db.base_class import PG_UUID, Base
from app.domain.media.model import Image
from app.domain.users.model import User

post_hashtags = Table(
    "post_hashtags",
    Base.metadata,
    Column("post_id", PG_UUID, ForeignKey("posts.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "hashtag_id",
        Integer,
        ForeignKey("hashtags.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class Hashtag(Base):
    __tablename__ = "hashtags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)

    __table_args__ = (
        Index(
            "idx_hashtags_name_gin",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
            postgresql_with={"fastupdate": True},
        ),
    )


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[UUID] = mapped_column(PG_UUID, primary_key=True, default=new_uuid7)
    user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    view_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    like_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    comment_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    report_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_blinded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    __mapper_args__ = {"version_id_col": version}

    __table_args__ = (
        # pg_trgm GIN: ILIKE '%term%' 시 Bitmap Index Scan 가능 (EXPLAIN으로 idx_posts_*_gin 확인).
        Index(
            "idx_posts_title_gin",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
            postgresql_with={"fastupdate": True},
        ),
        Index(
            "idx_posts_content_gin",
            "content",
            postgresql_using="gin",
            postgresql_ops={"content": "gin_trgm_ops"},
            postgresql_with={"fastupdate": True},
        ),
        Index(
            "idx_posts_feed_latest",
            created_at.desc(),
            postgresql_where=(deleted_at.is_(None)) & (is_blinded.is_(False)),
        ),
    )

    user: Mapped[User | None] = relationship(User, foreign_keys=[user_id], lazy="raise_on_sql")
    category: Mapped[Category | None] = relationship(
        "Category", foreign_keys=[category_id], lazy="raise_on_sql"
    )
    post_images: Mapped[list["PostImage"]] = relationship(
        "PostImage",
        back_populates="post",
        order_by="PostImage.id",
        lazy="raise_on_sql",
    )
    hashtags: Mapped[list[Hashtag]] = relationship(
        "Hashtag",
        secondary=post_hashtags,
        lazy="raise_on_sql",
    )


class PostImage(Base):
    __tablename__ = "post_images"
    __table_args__ = (UniqueConstraint("image_id", name="uq_post_images_image_id"),)

    id: Mapped[UUID] = mapped_column(PG_UUID, primary_key=True, default=new_uuid7)
    post_id: Mapped[UUID] = mapped_column(
        PG_UUID, ForeignKey("posts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    image_id: Mapped[UUID] = mapped_column(
        PG_UUID, ForeignKey("images.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    post: Mapped[Post] = relationship("Post", back_populates="post_images", lazy="raise_on_sql")
    image: Mapped[Image] = relationship(Image, foreign_keys=[image_id], lazy="raise_on_sql")

    @property
    def file_url(self) -> str | None:
        return self.image.file_url if self.image else None
