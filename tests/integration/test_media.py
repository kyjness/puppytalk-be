import uuid
from datetime import timedelta

import pytest
from app.core.config import settings
from app.db.base_class import utc_now
from app.domain.media.model import Image
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


def _img(
    key: str,
    *,
    uploader_id: uuid.UUID | None = None,
    created_at=None,
    deleted_at=None,
) -> Image:
    """테스트용 이미지 행. 파일 키만 다르고 나머지는 어느 테스트에서도 상관없는 값이다."""
    return Image(
        file_key=key,
        file_url=f"http://example.test/{key}",
        content_type="image/png",
        size=1,
        uploader_id=uploader_id,
        created_at=created_at if created_at is not None else utc_now(),
        deleted_at=deleted_at,
    )


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


async def test_delete_image_soft_deletes_and_detaches_every_reference(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """`delete_image`를 실 DB로 확인한다 — 가짜 리포지터리는 메서드 이름만 맞으면 통과하므로
    실제 SQL이 참조를 끊는지는 증명하지 못한다.

    새 계약(backlog #44)은 셋이다:
    1. 스토리지를 **아예 건드리지 않는다** — 그래서 S3가 죽어 있어도 삭제가 성공한다.
    2. 행은 남고 `deleted_at`이 찍힌다.
    3. 하드 삭제일 때 FK 연쇄가 끊어주던 참조 3종을 **같은 트랜잭션에서 직접 끊는다** —
       users·dog_profiles의 profile_image_id는 NULL, post_images 행은 삭제.
       이게 안 되면 지운 이미지가 화면에 계속 보인다.
    """
    from datetime import date

    from app.common.exceptions import ImageNotFoundException
    from app.domain.dogs.model import DogProfile
    from app.domain.media.service import MediaService
    from app.domain.posts.model import Post, PostImage
    from app.domain.users.model import User

    now = utc_now()
    sfx = uuid.uuid4().hex[:8]

    def _user(tag: str) -> User:
        return User(
            email=f"{tag}-{sfx}@example.com",
            password="x",
            nickname=f"{tag}{sfx}",
            status="ACTIVE",
            created_at=now,
            updated_at=now,
        )

    owner, stranger = _user("owner"), _user("stranger")
    db_session.add_all([owner, stranger])
    await db_session.flush()

    # 참조 3종을 각각 물고 있는 이미지 + 남의 것으로 거부될 이미지
    prof_key = f"prof-{sfx}"
    dog_key = f"dog-{sfx}"
    post_key = f"post-{sfx}"
    keep_key = f"keep-{sfx}"
    prof_img = _img(prof_key, uploader_id=owner.id, created_at=now)
    dog_img = _img(dog_key, uploader_id=owner.id, created_at=now)
    post_img = _img(post_key, uploader_id=owner.id, created_at=now)
    keep_img = _img(keep_key, uploader_id=owner.id, created_at=now)
    db_session.add_all([prof_img, dog_img, post_img, keep_img])
    await db_session.flush()

    owner.profile_image_id = prof_img.id
    dog = DogProfile(
        owner_id=owner.id,
        name=f"개{sfx}",
        breed="믹스",
        gender="MALE",
        birth_date=date(2020, 1, 1),
        profile_image_id=dog_img.id,
        created_at=now,
        updated_at=now,
    )
    post = Post(
        user_id=owner.id,
        title=f"글{sfx}",
        content="본문",
        created_at=now,
        updated_at=now,
    )
    db_session.add_all([dog, post])
    await db_session.flush()
    link = PostImage(post_id=post.id, image_id=post_img.id, created_at=now)
    db_session.add(link)
    await db_session.flush()

    # commit 뒤에는 속성이 만료되므로 id를 지금 붙잡는다.
    owner_id, stranger_id, dog_id, post_id = owner.id, stranger.id, dog.id, post.id
    prof_id, dog_img_id, post_img_id, keep_id = (
        prof_img.id,
        dog_img.id,
        post_img.id,
        keep_img.id,
    )
    all_keys = [prof_key, dog_key, post_key, keep_key]
    await db_session.commit()

    async def _deleted_at(image_id: uuid.UUID):
        """조회 후 트랜잭션을 닫는다 — `delete_image`가 자기 `db.begin()`을 열어야 한다."""
        row = await db_session.execute(select(Image.deleted_at).where(Image.id == image_id))
        value = row.scalar_one_or_none()
        await db_session.commit()
        return value

    try:
        # 1) 스토리지가 죽어 있어도 삭제는 성공한다 — 요청 경로가 S3를 안 부르기 때문.
        def boom(_key: str) -> None:
            raise AssertionError("delete_image가 요청 경로에서 스토리지를 건드렸다")

        monkeypatch.setattr("app.domain.media.service.storage_delete", boom)

        for image_id in (prof_id, dog_img_id, post_img_id):
            await MediaService.delete_image(image_id, owner_id, db_session)
            assert await _deleted_at(image_id) is not None

        # 2) 참조 3종이 전부 끊겼다
        detached = await db_session.execute(
            select(User.profile_image_id).where(User.id == owner_id)
        )
        assert detached.scalar_one() is None, (
            "users.profile_image_id가 남으면 지운 사진이 계속 보인다"
        )
        dog_ref = await db_session.execute(
            select(DogProfile.profile_image_id).where(DogProfile.id == dog_id)
        )
        assert dog_ref.scalar_one() is None
        links = await db_session.execute(
            select(PostImage.id).where(PostImage.image_id == post_img_id)
        )
        assert links.scalar_one_or_none() is None, "post_images 행이 남으면 게시글에 계속 붙어 있다"
        await db_session.commit()

        # 3) 남의 이미지는 거부되고 멀쩡하다
        with pytest.raises(ImageNotFoundException):
            await MediaService.delete_image(keep_id, stranger_id, db_session)
        assert await _deleted_at(keep_id) is None

        # 4) 두 번 지우면 두 번째는 404 — 이미 지운 것을 다시 지웠다고 하지 않는다
        with pytest.raises(ImageNotFoundException):
            await MediaService.delete_image(prof_id, owner_id, db_session)
    finally:
        await db_session.execute(delete(PostImage).where(PostImage.post_id == post_id))
        await db_session.execute(delete(Post).where(Post.id == post_id))
        await db_session.execute(delete(DogProfile).where(DogProfile.id == dog_id))
        await db_session.execute(delete(Image).where(Image.file_key.in_(all_keys)))
        await db_session.execute(delete(User).where(User.id.in_([owner_id, stranger_id])))
        await db_session.commit()


async def test_sweeper_collects_soft_deleted_images_without_waiting_24h(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """소프트 삭제분은 24시간 고아 조건과 무관하게 다음 회차에 수거된다.

    24시간을 그대로 적용하면 사용자가 지운 이미지가 하루 동안 S3에 남아 직링크로 열린다.
    참조는 삭제 시점에 이미 끊겨 있으므로 더 기다릴 이유도 없다.
    """
    from app.domain.media.service import MediaService

    now = utc_now()
    sfx = uuid.uuid4().hex[:8]
    deleted_key, live_key = f"sweep-deleted-{sfx}", f"sweep-live-{sfx}"

    # created_at은 방금 — 24시간 고아 조건으로는 둘 다 안 걸린다. deleted_at만이 근거다.
    db_session.add_all(
        [
            _img(deleted_key, created_at=now, deleted_at=now),
            _img(live_key, created_at=now),
        ]
    )
    await db_session.commit()

    deleted_keys: list[str] = []
    monkeypatch.setattr("app.domain.media.service.storage_delete", deleted_keys.append)

    try:
        await MediaService.sweep_unused_images(db=db_session)
        assert deleted_keys == [deleted_key], (
            "소프트 삭제분이 24시간 갇히면 지운 사진이 계속 열린다"
        )

        rows = await db_session.execute(
            select(Image.file_key).where(Image.file_key.in_([deleted_key, live_key]))
        )
        assert set(rows.scalars().all()) == {live_key}
        await db_session.commit()
    finally:
        await db_session.execute(delete(Image).where(Image.file_key.in_([deleted_key, live_key])))
        await db_session.commit()


async def test_attach_validation_blocks_concurrent_soft_delete(db_session: AsyncSession):
    """첨부 검증(FOR SHARE)과 소프트 삭제는 **겹치지 못한다.**

    잠금 없이는 검증이 통과한 뒤 다른 요청의 삭제가 커밋되고, 그 뒤 참조 insert가 들어간다 —
    지운 이미지가 게시글에 붙어 있고 참조가 있으니 스위퍼가 영구히 수거 못 한다. 하드 삭제
    시절엔 FK가 막아 주던 경우다.

    두 세션으로 재현한다: A가 검증을 잡은 채 대기 → B의 삭제가 **막혀야** 하고 → A가 참조를
    넣고 커밋하면 → B가 풀리며 방금 넣은 참조까지 끊어야 한다.
    """
    import asyncio

    from app.domain.media.model import MediaRepository
    from app.domain.media.service import MediaService
    from app.domain.posts.model import Post, PostImage
    from app.domain.users.model import User

    from tests.integration.conftest import TestSessionLocal

    now = utc_now()
    sfx = uuid.uuid4().hex[:8]
    owner = User(
        email=f"race-{sfx}@example.com",
        password="x",
        nickname=f"race{sfx}",
        status="ACTIVE",
        created_at=now,
        updated_at=now,
    )
    db_session.add(owner)
    await db_session.flush()
    img = _img(f"race-{sfx}", uploader_id=owner.id, created_at=now)
    post = Post(user_id=owner.id, title=f"글{sfx}", content="본문", created_at=now, updated_at=now)
    db_session.add_all([img, post])
    await db_session.flush()
    owner_id, image_id, post_id = owner.id, img.id, post.id
    await db_session.commit()

    async with TestSessionLocal() as attacher, TestSessionLocal() as deleter:
        try:
            async with attacher.begin():
                await MediaRepository.assert_images_attachable([image_id], db=attacher)

                deleting = asyncio.create_task(
                    MediaService.delete_image(image_id, owner_id, deleter)
                )
                await asyncio.sleep(0.5)
                assert not deleting.done(), (
                    "검증이 잠금을 안 잡으면 삭제가 그 사이 커밋되고 참조가 뒤에 들어간다"
                )

                attacher.add(PostImage(post_id=post_id, image_id=image_id, created_at=now))
            # A 커밋 → B가 풀린다
            await asyncio.wait_for(deleting, timeout=5)

            links = await db_session.execute(
                select(PostImage.id).where(PostImage.image_id == image_id)
            )
            assert links.scalar_one_or_none() is None, "삭제가 검증 뒤에 붙은 참조까지 끊어야 한다"
            await db_session.commit()
        finally:
            await db_session.execute(delete(PostImage).where(PostImage.post_id == post_id))
            await db_session.execute(delete(Post).where(Post.id == post_id))
            await db_session.execute(delete(Image).where(Image.id == image_id))
            await db_session.execute(delete(User).where(User.id == owner_id))
            await db_session.commit()
