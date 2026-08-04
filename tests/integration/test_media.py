import uuid
from datetime import timedelta

import pytest
from app.core.config import settings
from app.db.base_class import utc_now
from app.domain.media.model import Image
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


async def test_cleanup_keeps_records_when_storage_delete_fails(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """스토리지 삭제 실패 이미지는 DB 레코드를 보존(고아 방지)하고, 성공분만 삭제된다(#1)."""
    old = utc_now() - timedelta(days=1)
    sfx = uuid.uuid4().hex[:8]
    ok_key, fail_key = f"cleanup-ok-{sfx}", f"cleanup-fail-{sfx}"
    ok = Image(
        file_key=ok_key,
        file_url="u1",
        content_type="image/png",
        size=1,
        uploader_id=None,
        created_at=old,
    )
    fail = Image(
        file_key=fail_key,
        file_url="u2",
        content_type="image/png",
        size=1,
        uploader_id=None,
        created_at=old,
    )
    db_session.add_all([ok, fail])
    await db_session.commit()

    def fake_storage_delete(file_key: str) -> None:
        if file_key == fail_key:
            raise RuntimeError("storage down")

    monkeypatch.setattr("app.domain.media.service.storage_delete", fake_storage_delete)

    from app.domain.media.service import MediaService

    try:
        deleted, failed = await MediaService.cleanup_expired_signup_images(
            db_session, task_id="test"
        )
        assert deleted >= 1
        assert fail_key in failed

        remaining = (
            (
                await db_session.execute(
                    select(Image.file_key).where(Image.file_key.in_([ok_key, fail_key]))
                )
            )
            .scalars()
            .all()
        )
        # 삭제 실패분만 남고, 성공분은 제거됨.
        assert set(remaining) == {fail_key}
    finally:
        # 남긴 만료-orphan 행이 다른 테스트로 새지 않도록 정리.
        await db_session.execute(delete(Image).where(Image.file_key.in_([ok_key, fail_key])))
        await db_session.commit()


async def test_cleanup_advances_past_failing_head(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """id 앞머리의 스토리지 실패 이미지가 뒤쪽 정상 이미지를 굶기지 않는다(keyset 전진)."""
    # 배치 1로 좁혀, '실패 머리 → 정상 꼬리'를 서로 다른 배치로 강제.
    monkeypatch.setattr(settings, "MEDIA_CLEANUP_BATCH_SIZE", 1)
    old = utc_now() - timedelta(days=1)
    sfx = uuid.uuid4().hex[:8]
    a = Image(
        file_key=f"cleanup-a-{sfx}",
        file_url="u",
        content_type="image/png",
        size=1,
        uploader_id=None,
        created_at=old,
    )
    b = Image(
        file_key=f"cleanup-b-{sfx}",
        file_url="u",
        content_type="image/png",
        size=1,
        uploader_id=None,
        created_at=old,
    )
    db_session.add_all([a, b])
    await db_session.flush()  # uuid7 PK 확정
    # 작은 id를 '실패 머리'로: 실패분이 앞머리에 있어도 커서가 전진해 꼬리에 도달함을 검증.
    head, tail = sorted((a, b), key=lambda x: x.id)
    await db_session.commit()

    def fake_storage_delete(file_key: str) -> None:
        if file_key == head.file_key:
            raise RuntimeError("storage down")

    monkeypatch.setattr("app.domain.media.service.storage_delete", fake_storage_delete)

    from app.domain.media.service import MediaService

    try:
        deleted, failed = await MediaService.cleanup_expired_signup_images(
            db_session, task_id="test"
        )
        assert deleted >= 1
        assert head.file_key in failed

        remaining = (
            (
                await db_session.execute(
                    select(Image.file_key).where(Image.file_key.in_([head.file_key, tail.file_key]))
                )
            )
            .scalars()
            .all()
        )
        # 실패한 머리는 남고, 커서가 그 뒤로 전진해 꼬리(정상)는 삭제됨.
        assert set(remaining) == {head.file_key}
    finally:
        await db_session.execute(
            delete(Image).where(Image.file_key.in_([head.file_key, tail.file_key]))
        )
        await db_session.commit()
