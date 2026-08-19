from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Select,
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

from app.common.exceptions import InvalidRequestException
from app.core.config import settings
from app.core.ids import new_uuid7
from app.db.base_class import PG_UUID, Base, utc_now
from app.db.statements import update_one_returning

# 이미지를 참조하는 세 테이블을 경량 구문으로 잡는다 — 참조 해제(soft_delete_image_if_owned)와
# 고아 판별(_unreferenced)이 같은 정의를 쓴다. ORM 모델을 import하면 media가
# users·dogs·posts 도메인에 역방향 의존을 지게 되므로 필요한 컬럼만 선언한다.
_USERS_T = table("users", column("profile_image_id", PG_UUID))
_DOGS_T = table("dog_profiles", column("profile_image_id", PG_UUID))
_POST_IMAGES_T = table("post_images", column("image_id", PG_UUID))


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
    # 스위퍼가 수거할 행. 두 가지가 여기로 모인다 — 사용자가 지운 이미지, 그리고 아직 확정되지
    # 않은 업로드(승격 전에 미리 만들어 둔 예약 행). 둘 다 "S3 객체가 있을 수도 있고 없을 수도
    # 있는데 앱은 더 안 쓴다"라 처리가 같다(ADR 0019).
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    __table_args__ = (
        # 스위퍼의 수거 패스(get_reclaimable_images)가 `deleted_at IS NOT NULL ORDER BY id`로
        # 훑는다. 부분 인덱스라 소수만 담고, id 키라 정렬 없이 앞부터 LIMIT까지 읽는다.
        # 선언과 마이그레이션 016이 같은 이름·조건이어야 autogenerate가 DROP을 만들지 않는다.
        Index("idx_images_reclaimable", "id", postgresql_where=deleted_at.is_not(None)),
    )


def _unreferenced() -> list[ColumnElement[bool]]:
    """어느 테이블도 이 이미지를 가리키지 않는다 — 참조 3종에 대한 anti-join 조건."""
    return [
        ~exists(select(1).select_from(_USERS_T).where(_USERS_T.c.profile_image_id == Image.id)),
        ~exists(select(1).select_from(_DOGS_T).where(_DOGS_T.c.profile_image_id == Image.id)),
        ~exists(select(1).select_from(_POST_IMAGES_T).where(_POST_IMAGES_T.c.image_id == Image.id)),
    ]


def _cleanup_page(
    stmt: Select[tuple[Image]], *, after_id: UUID | None, limit: int | None
) -> Select[tuple[Image]]:
    """정리 잡 전용 페이지 — id 오름차순 keyset + **`FOR UPDATE SKIP LOCKED`**.

    정리 루프는 이 페이지를 잡은 트랜잭션 안에서 스토리지를 지우고 행을 지운다. 잠금 없이는
    fetch와 행 삭제 사이에 다른 요청이 그 행을 바꿀 수 있다 — 확정 중인 예약 행을 지우거나
    (사용자는 200을 받았는데 행이 없다), 첨부 검증 중인 이미지를 지운다. 잠긴 행은 건너뛰고
    다음 회차가 본다 — 확정·첨부가 끝나면 상태가 바뀌어 있거나(수거 대상 아님) 그대로다.
    """
    if after_id is not None:
        stmt = stmt.where(Image.id > after_id)
    if limit is not None:
        stmt = stmt.limit(limit)
    return stmt.with_for_update(skip_locked=True)


class MediaRepository:
    @classmethod
    async def create_reserved_image(
        cls,
        file_key: str,
        file_url: str,
        content_type: str | None = None,
        size: int | None = None,
        uploader_id: UUID | None = None,
        *,
        db: AsyncSession,
    ) -> Image:
        """`deleted_at`을 찍은 채 만든다 — **예약 행**.

        업로드 확정은 이 행을 먼저 커밋한 뒤 S3로 승격한다. 승격이나 그 뒤 단계가 실패하면
        예약 상태 그대로 남아 스위퍼가 S3 객체와 행을 함께 회수한다. 덕분에 "행이 존재한 적
        없는 S3 객체"가 생길 수 없다(ADR 0019).

        이미지 행이 생기는 경로는 이것 하나뿐이다 — 확정되지 않은 행은 존재할 수 있어도
        그 반대(S3에만 있는 객체)는 존재할 수 없어야 하므로, 예약을 건너뛰는 생성자를 두지 않는다.
        """
        img = Image(
            file_key=file_key,
            file_url=file_url,
            content_type=content_type,
            size=size,
            uploader_id=uploader_id,
            created_at=utc_now(),
            deleted_at=utc_now(),
        )
        db.add(img)
        await db.flush()
        return img

    @classmethod
    async def lock_reserved_image(cls, image_id: UUID, *, db: AsyncSession) -> Image | None:
        """예약 행을 `FOR UPDATE`로 잡는다 — 승격부터 확정까지 스위퍼가 이 행을 못 건드리게.

        스위퍼의 수거 페이지는 `SKIP LOCKED`라 잠긴 행을 건너뛰고, 스위퍼가 먼저 잡았으면
        여기서 기다렸다가 None을 본다(행이 지워졌다). 어느 쪽도 겹치지 않는다.
        `deleted_at IS NOT NULL` — 예약 상태가 아니면 잠글 대상이 아니다.
        """
        r = await db.execute(
            select(Image)
            .where(Image.id == image_id, Image.deleted_at.is_not(None))
            .with_for_update()
        )
        return r.scalars().one_or_none()

    @classmethod
    async def confirm_reserved_image(
        cls,
        image_id: UUID,
        *,
        size: int | None,
        content_type: str | None,
        db: AsyncSession,
    ) -> Image | None:
        """예약 행을 확정한다 — `deleted_at`을 지우고 승격이 실제로 확인한 메타를 반영한다.

        `deleted_at IS NOT NULL`을 조건에 두어, 스위퍼가 먼저 회수해 간 행을 되살리지 않는다.
        """
        return await update_one_returning(
            db,
            Image,
            [Image.id == image_id, Image.deleted_at.is_not(None)],
            {"deleted_at": None, "size": size, "content_type": content_type},
            Image,
        )

    # 아래 둘은 **첨부 검증** 경로다 — 새 게시글·프로필에 이미지를 붙여도 되는지 판단한다.
    # 둘 다 `deleted_at IS NULL`을 건다: 걸지 않으면 수거를 기다리는 행을 다시 붙일 수 있고,
    # 그러면 스위퍼가 곧 지울 객체를 참조하는 게시글이 생긴다.
    # 표시 경로는 필터가 필요 없다 — 소프트 삭제가 FK 참조를 같은 트랜잭션에서 끊으므로
    # (`soft_delete_image_if_owned`) 조인이 애초에 이 행에 닿지 않는다.
    @classmethod
    async def assert_images_attachable(cls, image_ids: list[UUID], *, db: AsyncSession) -> None:
        """전부 붙일 수 있는 이미지인지 확인하고, **호출부 트랜잭션이 끝날 때까지 잠근다.**

        게시글·프로필·개 프로필이 이미지를 참조하기 전에 거치는 유일한 문이다. 하나로 모은
        이유는 dogs가 이 검증을 안 거친 채 배포된 적이 있어서다 — 문이 셋이면 하나를 빼먹는다.

        `FOR SHARE`가 핵심이다. 검증(읽기)과 참조 insert 사이에 다른 요청의 소프트 삭제가
        커밋되면, 그 CTE는 아직 없는 참조를 못 끊고 그 뒤 insert가 들어간다 — 지운 이미지가
        게시글에 붙고, 참조가 있으니 스위퍼가 영구히 수거 못 한다. 하드 삭제 시절엔 FK가
        막아 주던 것이다. `FOR SHARE`는 소프트 삭제의 UPDATE(FOR NO KEY UPDATE)와 충돌하므로
        어느 쪽이 먼저든 뒤가 기다린다: 검증이 먼저면 삭제가 insert 커밋 뒤에 돌아 그 참조까지
        끊고, 삭제가 먼저면 검증이 `deleted_at`을 보고 거부한다. `FOR KEY SHARE`로는 안 된다 —
        NO KEY UPDATE와 호환돼 막지 못한다.

        호출부는 반드시 `db.begin()` 안이어야 잠금이 insert까지 산다.
        """
        if not image_ids:
            return
        wanted = set(image_ids)
        r = await db.execute(
            select(Image.id)
            .where(Image.id.in_(wanted), Image.deleted_at.is_(None))
            .with_for_update(read=True)
        )
        if set(r.scalars().all()) != wanted:
            raise InvalidRequestException("업로드되지 않은 이미지 ID를 참조할 수 없습니다.")

    @classmethod
    async def claim_image_ownership(cls, image_id: UUID, user_id: UUID, db: AsyncSession) -> bool:
        r = await db.execute(
            update(Image)
            .where(Image.id == image_id)
            .where(Image.uploader_id.is_(None))
            .where(Image.deleted_at.is_(None))
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
                # 예약 행(deleted_at 찍힘)은 수거 패스 소관 — 두 잡이 같은 행을 다투지 않는다.
                Image.deleted_at.is_(None),
                Image.created_at < cutoff,
            )
            .order_by(Image.id.asc())
        )
        result = await db.execute(_cleanup_page(stmt, after_id=after_id, limit=limit))
        return list(result.scalars().all())

    # 수거 대상은 두 종류고 **쿼리도 둘로 나눠 둔다.** 하나로 합치면 조건이
    # `created_at < X OR deleted_at ...`가 되는데, `created_at`엔 인덱스가 없어 Postgres가
    # BitmapOr를 못 만들고 전체 스캔으로 떨어진다 — `deleted_at` 부분 인덱스가 있어도 안 쓰인다.
    @classmethod
    async def get_abandoned_images(
        cls,
        *,
        older_than_hours: int,
        db: AsyncSession,
        limit: int | None = None,
        after_id: UUID | None = None,
    ) -> list[Image]:
        """**버려진 업로드** — 확정된 지 오래됐는데 아무도 안 쓰는 것."""
        cutoff = utc_now() - timedelta(hours=older_than_hours)
        stmt = (
            select(Image)
            .where(Image.created_at < cutoff, Image.deleted_at.is_(None))
            .where(*_unreferenced())
            .order_by(Image.id.asc())
        )
        result = await db.execute(_cleanup_page(stmt, after_id=after_id, limit=limit))
        return list(result.scalars().all())

    @classmethod
    async def get_reclaimable_images(
        cls,
        *,
        grace_seconds: int,
        db: AsyncSession,
        limit: int | None = None,
        after_id: UUID | None = None,
    ) -> list[Image]:
        """**`deleted_at`이 찍힌 행** — 24시간을 기다리지 않는다.

        참조는 삭제 시점에 이미 끊겨 있고, 지운 이미지가 하루 동안 S3에 남아 직링크로
        열리면 안 된다. `idx_images_reclaimable` 부분 인덱스를 타므로 작업량이 `images`
        전체가 아니라 **수거 대상 수**에 비례한다(`deleted_at < X`는 `IS NOT NULL`을
        함의하므로 부분 인덱스 조건이 성립한다).

        `grace_seconds`는 **기본값을 주지 않는다.** 업로드 확정이 예약 행(`deleted_at`을
        찍은 채)을 먼저 만들고 S3 승격 후 확정하므로, 유예가 0이면 승격 중인 행과 방금 올린
        객체를 스위퍼가 지운다 — 사용자는 성공 응답을 받았는데 이미지가 사라진다.
        데이터 계층이 그 사고를 기본값으로 들고 있으면 안 된다.
        """
        stmt = (
            select(Image)
            .where(Image.deleted_at < utc_now() - timedelta(seconds=grace_seconds))
            .where(*_unreferenced())
            .order_by(Image.id.asc())
        )
        result = await db.execute(_cleanup_page(stmt, after_id=after_id, limit=limit))
        return list(result.scalars().all())

    @classmethod
    async def soft_delete_image_if_owned(
        cls,
        image_id: UUID,
        user_id: UUID,
        *,
        db: AsyncSession,
    ) -> bool:
        """소유자면 `deleted_at`을 찍고 **참조를 같은 트랜잭션에서 끊는다**. 반환 = 실제로 찍었는가.

        참조 해제 세 줄은 하드 삭제일 때 FK 연쇄(`SET NULL`·`CASCADE`)가 해주던 일을 그대로
        옮긴 것이다 — 소프트 삭제는 행을 남기므로 연쇄가 안 걸린다. 이걸 여기서 하기 때문에
        **표시 경로 쿼리를 하나도 고치지 않아도** 지운 이미지가 화면에서 사라진다.

        부수 효과로 이 행은 참조가 없어져 기존 고아 판별식을 그대로 통과한다 —
        스위퍼는 `deleted_at` 조건만 보면 된다.

        **문장은 둘이다 — 하나로 합치면 안 된다.** 첫 문장(`UPDATE images`)은 첨부 검증의
        `FOR SHARE`(`assert_images_attachable`)에 막혀 그 트랜잭션이 끝날 때까지 기다린 뒤 진행한다.
        참조 해제가 그 뒤의 **별도 문장**이어야 새 스냅샷으로 방금 붙은 참조를 본다. 네 개를
        data-modifying CTE 한 문장으로 묶으면 WITH 안의 부문장이 전부 문장 시작 시점의 스냅샷을
        쓰므로, 잠금을 기다리는 동안 커밋된 참조를 못 보고 지나친다 — 테스트로 확인했다
        (`test_attach_validation_blocks_concurrent_soft_delete`).

        둘째 문장은 참조 해제 셋을 CTE로 묶어 왕복 1번이다. 합쳐서 2왕복 — 4문장을 따로 보내던
        것보다는 적고, 잠금 시맨틱을 지키는 최소다.
        """
        r = await db.execute(
            update(Image)
            .where(
                Image.id == image_id,
                Image.uploader_id == user_id,
                Image.deleted_at.is_(None),
            )
            .values(deleted_at=utc_now())
            .returning(Image.id)
        )
        if r.scalar_one_or_none() is None:
            return False

        detach_user = (
            update(_USERS_T)
            .where(_USERS_T.c.profile_image_id == image_id)
            .values(profile_image_id=None)
            .cte("detach_user")
        )
        detach_dog = (
            update(_DOGS_T)
            .where(_DOGS_T.c.profile_image_id == image_id)
            .values(profile_image_id=None)
            .cte("detach_dog")
        )
        detach_post = (
            delete(_POST_IMAGES_T).where(_POST_IMAGES_T.c.image_id == image_id).cte("detach_post")
        )
        await db.execute(select(1).add_cte(detach_user, detach_dog, detach_post))
        return True
