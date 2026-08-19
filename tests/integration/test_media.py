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


async def test_sweeper_collects_deleted_rows_but_spares_ones_still_in_grace(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """`deleted_at`이 찍힌 행의 수거 규칙 — 24시간은 안 기다리되, 유예는 지킨다.

    - 24시간을 그대로 적용하면 지운 사진이 하루 동안 S3에 남아 직링크로 열린다.
    - 반대로 유예가 없으면 확정 중인 **예약 행**(승격 진행 중)을 지워버려, 사용자가 성공
      응답을 받은 이미지가 사라진다.
    """
    from app.domain.media.service import MediaService

    now = utc_now()
    sfx = uuid.uuid4().hex[:8]
    ripe_key = f"sweep-ripe-{sfx}"
    fresh_key = f"sweep-fresh-{sfx}"
    live_key = f"sweep-live-{sfx}"
    keys = [ripe_key, fresh_key, live_key]
    # created_at은 방금 — 24시간 고아 조건으로는 셋 다 안 걸린다. deleted_at만이 근거다.
    # 유예는 설정값을 읽는다. 숫자를 박아 두면 설정을 바꿨을 때 테스트가 조용히 무의미해진다.
    ripe_at = now - timedelta(seconds=settings.RESERVED_IMAGE_GRACE_SECONDS + 60)
    db_session.add_all(
        [
            _img(ripe_key, created_at=now, deleted_at=ripe_at),  # 유예 지남 → 수거
            _img(fresh_key, created_at=now, deleted_at=now),  # 방금 찍힘(확정 중) → 보존
            _img(live_key, created_at=now),  # 멀쩡한 이미지 → 보존
        ]
    )
    await db_session.commit()

    deleted_keys: list[str] = []
    monkeypatch.setattr("app.domain.media.service.storage_delete", deleted_keys.append)

    try:
        await MediaService.sweep_unused_images(db=db_session)
        assert deleted_keys == [ripe_key], "유예 안의 행을 건드리면 확정 중인 업로드가 사라진다"

        rows = await db_session.execute(select(Image.file_key).where(Image.file_key.in_(keys)))
        assert set(rows.scalars().all()) == {fresh_key, live_key}
        await db_session.commit()
    finally:
        await db_session.execute(delete(Image).where(Image.file_key.in_(keys)))
        await db_session.commit()


async def test_confirm_leaves_reclaimable_row_when_promote_fails(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """업로드 확정이 도중에 실패해도 **행이 남아** 스위퍼가 회수할 수 있다.

    이 순서(행 → S3)가 아니면 승격과 행 생성 사이에 "행 없는 S3 객체"가 존재하는 창이 생기고,
    보상 삭제마저 실패하면 행 기준 스위퍼가 원리적으로 못 보는 객체가 영구히 남는다.
    실 DB로 확인하는 이유는 예약 행이 **커밋**되어야만 회수 가능하기 때문이다 — 가짜 세션은
    그 차이를 못 본다.
    """
    from app.common.exceptions import InvalidImageFileException
    from app.core.ids import new_uuid7
    from app.domain.media import service as media_service_mod
    from app.domain.media.image_policy import build_pending_file_key
    from app.domain.media.service import MediaService

    pending_key = build_pending_file_key(new_uuid7(), "x.png")

    async def fake_head(_key):
        return {"ContentLength": 100, "ContentType": "image/png", "ETag": '"e"'}

    # 승격 대상 키를 여기서 붙잡는다 — `post/%` 전체를 훑어 before/after를 비교하면 다른
    # 테스트가 남긴 행에 얽힌다.
    promoted_to: list[str] = []

    async def failing_promote(_key, dest_key, *, etag):
        promoted_to.append(dest_key)
        raise ValueError("S3 copy failed")

    monkeypatch.setattr(media_service_mod, "head_pending_object", fake_head)
    monkeypatch.setattr(media_service_mod, "promote_pending_object", failing_promote)

    with pytest.raises(InvalidImageFileException):
        async with MediaService._reserved_upload(
            pending_key, purpose="post", expected_size=100, uploader_id=None, db=db_session
        ):
            pass

    assert len(promoted_to) == 1
    dest_key = promoted_to[0]
    try:
        row = await db_session.execute(select(Image.deleted_at).where(Image.file_key == dest_key))
        deleted_at = row.scalar_one_or_none()
        await db_session.commit()
        assert deleted_at is not None, "승격 실패 후 예약 행이 없으면 S3 잔존물을 영영 못 찾는다"
    finally:
        await db_session.execute(delete(Image).where(Image.file_key == dest_key))
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


async def test_sweeper_skips_reserved_row_while_upload_holds_it(
    db_session: AsyncSession, monkeypatch
):
    """확정 중인 예약 행은 유예를 넘겼어도 스위퍼가 건드리지 않는다 — 행 잠금(SKIP LOCKED).

    유예는 확률적 방어일 뿐이다. 승격이 유예보다 오래 걸리면 스위퍼가 그 행을 집는 순간
    확정이 성공해, 200 받은 이미지의 행이 지워지거나 객체만 지워진다. 업로드가 승격부터
    확정까지 행을 `FOR UPDATE`로 잡고 있고 스위퍼가 잠긴 행을 건너뛰면 둘은 겹칠 수 없다.
    """
    from app.domain.media.model import MediaRepository
    from app.domain.media.service import MediaService

    from tests.integration.conftest import TestSessionLocal

    now = utc_now()
    key = f"sweep-locked-{uuid.uuid4().hex[:8]}"
    stale = now - timedelta(seconds=settings.RESERVED_IMAGE_GRACE_SECONDS + 60)
    img = _img(key, created_at=now, deleted_at=stale)  # 유예를 한참 넘긴 예약 행
    db_session.add(img)
    await db_session.commit()
    image_id = img.id

    deleted_keys: list[str] = []
    monkeypatch.setattr("app.domain.media.service.storage_delete", deleted_keys.append)

    try:
        async with TestSessionLocal() as uploader:
            async with uploader.begin():
                assert await MediaRepository.lock_reserved_image(image_id, db=uploader) is not None
                # 업로드가 잡고 있는 동안 — 스위퍼는 이 행을 못 본다
                await MediaService.sweep_unused_images(db=db_session)
                assert key not in deleted_keys, "잠긴 예약 행을 스위퍼가 집으면 확정과 겹친다"
        # 업로드가 놓았다(확정 안 하고 롤백) — 이제 수거 대상이다
        await MediaService.sweep_unused_images(db=db_session)
        assert key in deleted_keys
    finally:
        await db_session.execute(delete(Image).where(Image.id == image_id))
        await db_session.commit()
