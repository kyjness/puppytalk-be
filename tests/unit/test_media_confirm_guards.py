"""presigned confirm 견고성 + 인증 presign 유저 한도 단위 테스트.

핵심 불변식: confirm 검증(size·content-type)은 promote(영구 copy + pending 삭제) **앞**에서
실행된다 — 승격 후 거부는 DB 행 없는 영구 객체를 남겨 sweeper(DB 행 기준)·pending/ lifecycle
어느 쪽도 못 지운다. head~promote 사이 재업로드(TOCTOU)로 우회된 경우엔 승격본을 보상 삭제한다.
미업로드/소진된 키의 404는 500이 아니라 400으로 매핑된다.

같은 불변식이 삭제 경로에도 걸린다 — `delete_image`는 스토리지를 먼저 지우고 DB 행을
나중에 지운다. 순서가 반대면 스토리지 삭제 실패 시 추적 수단(행)이 사라져 회수가 불가능하다.
"""

import uuid
from typing import Any

import pytest
from app.common.exceptions import InvalidImageFileException
from app.domain.media import service as media_service_mod
from app.domain.media.service import MediaService

from tests.unit.fakes import FakeDB, as_session

pytestmark = pytest.mark.asyncio


def _valid_pending_key() -> str:
    from app.core.ids import new_uuid7

    return f"pending/{new_uuid7()}/x.png"


def _meta(size: int = 100, content_type: str = "image/png") -> dict[str, Any]:
    return {"ContentLength": size, "ContentType": content_type}


async def test_confirm_rejects_size_mismatch_before_promote(monkeypatch):
    async def fake_head(key):
        return _meta(size=100)

    async def fail_promote(key, purpose, **_):
        raise AssertionError("검증 실패 시 promote(영구 copy + pending 삭제)가 실행되면 안 된다")

    monkeypatch.setattr(media_service_mod, "head_pending_object", fake_head)
    monkeypatch.setattr(media_service_mod, "promote_pending_object", fail_promote)

    with pytest.raises(InvalidImageFileException):
        await MediaService._confirm_pending_key(
            _valid_pending_key(), purpose="post", expected_size=99
        )


async def test_confirm_rejects_disallowed_type_before_promote(monkeypatch):
    """storage 계층은 webp를 승격할 수 있지만 정책(ALLOWED_IMAGE_TYPES)이 거부한다 —
    이 거부가 promote 앞이어야 영구 객체가 안 남는다."""
    from app.common.exceptions import InvalidFileTypeException
    from app.core.config import settings

    monkeypatch.setattr(settings, "ALLOWED_IMAGE_TYPES", ["image/jpeg", "image/png"])

    async def fake_head(key):
        return _meta(content_type="image/webp")

    async def fail_promote(key, purpose, **_):
        raise AssertionError("정책 거부 대상이 promote되면 안 된다")

    monkeypatch.setattr(media_service_mod, "head_pending_object", fake_head)
    monkeypatch.setattr(media_service_mod, "promote_pending_object", fail_promote)

    with pytest.raises(InvalidFileTypeException):
        await MediaService._confirm_pending_key(
            _valid_pending_key(), purpose="post", expected_size=100
        )


async def test_confirm_maps_missing_object_to_400(monkeypatch):
    """미업로드·이미 소진(승격)된 1회성 키 → head의 ValueError가 400 계열로 매핑된다(500 금지)."""

    async def fake_head(key):
        raise ValueError("pending object not found")

    monkeypatch.setattr(media_service_mod, "head_pending_object", fake_head)

    with pytest.raises(InvalidImageFileException):
        await MediaService._confirm_pending_key(
            _valid_pending_key(), purpose="post", expected_size=None
        )


async def test_confirm_deletes_promoted_object_when_recheck_fails(monkeypatch):
    """head~promote 사이 재업로드로 선검증을 우회한 경우 — 승격 결과 재확인 실패 시
    승격본을 보상 삭제해 누수 없이 거부한다."""
    from app.common.exceptions import InvalidFileTypeException
    from app.core.config import settings

    monkeypatch.setattr(settings, "ALLOWED_IMAGE_TYPES", ["image/jpeg", "image/png"])
    deleted: list[str] = []

    async def fake_head(key):
        return _meta(size=100, content_type="image/png")

    async def fake_promote(key, purpose, **_):
        return f"{purpose}/swapped.webp", 100, "image/webp"  # 승격 시점엔 webp로 바뀜

    def fake_delete(key):
        deleted.append(key)

    monkeypatch.setattr(media_service_mod, "head_pending_object", fake_head)
    monkeypatch.setattr(media_service_mod, "promote_pending_object", fake_promote)
    monkeypatch.setattr(media_service_mod, "storage_delete", fake_delete)

    with pytest.raises(InvalidFileTypeException):
        await MediaService._confirm_pending_key(
            _valid_pending_key(), purpose="post", expected_size=100
        )
    assert deleted == ["post/swapped.webp"]


async def test_confirm_happy_path(monkeypatch):
    async def fake_head(key):
        return _meta(size=100, content_type="image/png")

    async def fake_promote(key, purpose, **_):
        return f"{purpose}/ok.png", 100, "image/png"

    monkeypatch.setattr(media_service_mod, "head_pending_object", fake_head)
    monkeypatch.setattr(media_service_mod, "promote_pending_object", fake_promote)
    monkeypatch.setattr(media_service_mod, "build_url", lambda k: f"https://cdn/{k}")

    dest_key, url, content_type, size = await MediaService._confirm_pending_key(
        _valid_pending_key(), purpose="post", expected_size=100
    )
    assert (dest_key, url, content_type, size) == (
        "post/ok.png",
        "https://cdn/post/ok.png",
        "image/png",
        100,
    )


# --- storage head: 404 → ValueError 매핑 ---


def test_head_pending_maps_client_error_404_to_value_error(monkeypatch):
    from app.infra import storage as storage_mod
    from botocore.exceptions import ClientError

    class _FakeClient:
        def head_object(self, **kwargs):
            raise ClientError(
                {"ResponseMetadata": {"HTTPStatusCode": 404}, "Error": {"Code": "404"}},
                "HeadObject",
            )

    monkeypatch.setattr(storage_mod, "_get_s3_client", lambda: _FakeClient())
    with pytest.raises(ValueError):
        storage_mod._head_pending_object_sync(_valid_pending_key())


def test_head_pending_propagates_non_404_client_error(monkeypatch):
    """권한·장애 계열 ClientError는 400으로 가장하지 않고 5xx로 남긴다."""
    from app.infra import storage as storage_mod
    from botocore.exceptions import ClientError

    class _FakeClient:
        def head_object(self, **kwargs):
            raise ClientError(
                {"ResponseMetadata": {"HTTPStatusCode": 403}, "Error": {"Code": "403"}},
                "HeadObject",
            )

    monkeypatch.setattr(storage_mod, "_get_s3_client", lambda: _FakeClient())
    with pytest.raises(ClientError):
        storage_mod._head_pending_object_sync(_valid_pending_key())


# --- 인증 presign 유저 단위 한도 ---


async def test_presign_rate_limited_per_user(monkeypatch):
    """인증 presign은 유저 단위 fixed-window — 초과 시 429(TooManyRequestsException)."""
    from types import SimpleNamespace

    from app.common.exceptions import TooManyRequestsException
    from app.core.config import settings
    from app.domain.media import router as media_router_mod
    from app.domain.media.schema import PresignUploadRequest

    monkeypatch.setattr(settings, "MEDIA_PRESIGN_RATE_LIMIT_MAX", 1)

    issued: list[str] = []

    async def fake_issue(body):
        issued.append(body.filename)
        return SimpleNamespace(url="u", fields={}, file_key="k")

    monkeypatch.setattr(
        media_router_mod.MediaService, "issue_presigned_upload", staticmethod(fake_issue)
    )
    monkeypatch.setattr(media_router_mod, "api_response", lambda request, **kw: kw["data"])

    user = SimpleNamespace(id=uuid.uuid4())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)))
    body = PresignUploadRequest(filename="a.png", content_type="image/png")

    ok = await media_router_mod.presign_image_upload(request, body, user)  # type: ignore[arg-type]
    assert issued == ["a.png"]

    with pytest.raises(TooManyRequestsException) as exc:
        await media_router_mod.presign_image_upload(request, body, user)  # type: ignore[arg-type]
    assert exc.value.status_code == 429
    assert issued == ["a.png"]  # 한도 초과 시 presign 미발급
    assert ok is not None


# --- 삭제 경로: 스토리지 실패 시 DB 행을 남긴다 ---


async def test_delete_image_keeps_db_row_when_storage_delete_fails(monkeypatch):
    """스토리지 삭제가 실패하면 DB 행을 지우면 안 된다.

    행이 유일한 추적 수단이라(고아 sweeper는 `images` 행 기준, `pending/` lifecycle은
    `media/`를 안 덮는다) 행을 먼저 지우면 아무도 못 찾는 객체가 영구히 남는다.
    행이 남으면 사용자 재시도·sweeper 회수가 모두 가능하다.
    """
    image_id = uuid.uuid4()
    user_id = uuid.uuid4()
    deleted_ids: list[Any] = []

    class _Image:
        id = image_id
        uploader_id = user_id
        file_key = "media/some/key.png"

    class _Repo:
        @staticmethod
        async def get_image_by_id(iid, db):
            return _Image()

        @staticmethod
        async def delete_images_by_ids(ids, db):
            deleted_ids.extend(ids)
            return len(ids)

    def boom(_key):
        raise RuntimeError("S3 down")

    monkeypatch.setattr(media_service_mod, "MediaRepository", _Repo)
    monkeypatch.setattr(media_service_mod, "storage_delete", boom)

    with pytest.raises(RuntimeError):
        await MediaService.delete_image(image_id, user_id, as_session(FakeDB()))

    assert deleted_ids == [], "스토리지 삭제 실패 후 DB 행이 지워지면 객체를 영영 회수할 수 없다"


async def test_delete_image_removes_db_row_after_storage_succeeds(monkeypatch):
    """정상 경로 회귀 — 스토리지 삭제가 성공하면 행도 지워진다."""
    image_id = uuid.uuid4()
    user_id = uuid.uuid4()
    deleted_ids: list[Any] = []
    deleted_keys: list[str] = []

    class _Image:
        id = image_id
        uploader_id = user_id
        file_key = "media/some/key.png"

    class _Repo:
        @staticmethod
        async def get_image_by_id(iid, db):
            return _Image()

        @staticmethod
        async def delete_images_by_ids(ids, db):
            deleted_ids.extend(ids)
            return len(ids)

    monkeypatch.setattr(media_service_mod, "MediaRepository", _Repo)
    monkeypatch.setattr(media_service_mod, "storage_delete", deleted_keys.append)

    await MediaService.delete_image(image_id, user_id, as_session(FakeDB()))

    assert deleted_keys == ["media/some/key.png"]
    assert deleted_ids == [image_id]


async def test_signup_confirm_rollback_keeps_db_row_when_storage_delete_fails(monkeypatch):
    """signup confirm 롤백도 스토리지 실패 시 DB 행을 남겨야 한다.

    `create_temp_image`는 커밋되므로 실제 행이 존재한다. 행을 먼저 지우고 스토리지 삭제가
    실패하면 회수 수단이 사라진다 — temp image는 어디에도 연결되지 않아 행만 남아 있으면
    고아 sweeper가 24시간 뒤 정리한다.
    """
    from app.domain.media.schema import ConfirmSignupUploadRequest

    image_id = uuid.uuid4()
    deleted_ids: list[Any] = []

    class _Image:
        id = image_id
        file_url = "http://example.test/media/x.png"

    class _Repo:
        @staticmethod
        async def create_temp_image(**_kwargs):
            return _Image()

        @staticmethod
        async def delete_images_by_ids(ids, db):
            deleted_ids.extend(ids)
            return len(ids)

    async def fake_confirm(key, *, purpose, expected_size):
        return "media/dest/x.png", _Image.file_url, "image/png", 100

    async def fail_token(image_id_, redis):
        raise RuntimeError("redis down")  # 롤백 유발

    def boom(_key):
        raise RuntimeError("S3 down")

    monkeypatch.setattr(MediaService, "_confirm_pending_key", fake_confirm)
    monkeypatch.setattr(MediaService, "issue_upload_token", fail_token)
    monkeypatch.setattr(media_service_mod, "MediaRepository", _Repo)
    monkeypatch.setattr(media_service_mod, "storage_delete", boom)

    with pytest.raises(RuntimeError):
        await MediaService.confirm_presigned_signup_upload(
            ConfirmSignupUploadRequest(file_key=_valid_pending_key(), size=100),
            as_session(FakeDB()),
            None,
        )

    assert deleted_ids == [], "스토리지 삭제 실패 후 행을 지우면 고아를 영영 회수할 수 없다"
