"""presigned confirm 견고성 + 인증 presign 유저 한도 단위 테스트.

핵심 불변식: confirm 검증(size·content-type)은 promote(영구 copy + pending 삭제) **앞**에서
실행된다 — 승격 후 거부는 DB 행 없는 영구 객체를 남겨 sweeper(DB 행 기준)·pending/ lifecycle
어느 쪽도 못 지운다. head~promote 사이 재업로드(TOCTOU)로 우회된 경우엔 승격본을 보상 삭제한다.
미업로드/소진된 키의 404는 500이 아니라 400으로 매핑된다.
"""

import uuid
from typing import Any

import pytest
from app.common.exceptions import InvalidImageFileException
from app.domain.media import service as media_service_mod
from app.domain.media.service import MediaService

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
