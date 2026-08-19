"""presigned confirm 견고성 + 인증 presign 유저 한도 단위 테스트.

핵심 불변식(ADR 0019): **S3에 객체가 있으면 그것을 가리키는 `images` 행이 먼저 있다.**
확정은 예약 행(`deleted_at` 찍힌 채)을 커밋한 뒤 승격하고, 실패하면 예약 상태로 남겨 스위퍼에
넘긴다 — 보상 삭제를 쓰지 않는다. 보상은 그 자체로 실패할 수 있고, 실패하면 행 기준 스위퍼가
원리적으로 못 보는 객체가 영구히 남는다.

확정은 **호출부의 마지막 단계**다. 확정 뒤에 할 일이 남으면 그게 실패했을 때 되돌리는 쓰기가
필요해지고, 방금 없앤 보상 쓰기가 S3 대신 DB로 되살아난다.

값싼 거부(size·content-type)는 여전히 승격 **앞**에서 한다 — 거부가 `pending/`에서 끝나면
lifecycle이 회수하므로 예약 행조차 만들 필요가 없다. 미업로드/소진된 키의 404는 400으로 매핑.

삭제 경로도 같은 원리다 — 요청 안에서는 DB만 건드리고 스토리지는 스위퍼가 맡는다.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from app.common.exceptions import InvalidImageFileException
from app.domain.media import service as media_service_mod
from app.domain.media.service import MediaService

from tests.unit.fakes import FakeDB, as_session

pytestmark = pytest.mark.asyncio


def _valid_pending_key() -> str:
    # 키 형식은 `is_valid_pending_file_key` 정규식과 묶여 있다 — 리터럴로 박으면 조용히 썩는다.
    from app.core.ids import new_uuid7
    from app.domain.media.image_policy import build_pending_file_key

    return build_pending_file_key(new_uuid7(), "x.png")


def _meta(size: int = 100, content_type: str = "image/png") -> dict[str, Any]:
    return {"ContentLength": size, "ContentType": content_type, "ETag": '"etag-1"'}


class _ReservingRepo:
    """예약 → 잠금 → 확정 흐름을 기록하는 가짜 리포지터리."""

    def __init__(self, *, reclaimed: bool = False) -> None:
        self.reserved: list[str] = []
        self.locked: list[Any] = []
        self.confirmed: list[Any] = []
        self.reclaimed = reclaimed  # True면 잠그려 할 때 이미 스위퍼가 가져간 것처럼 군다

    async def create_reserved_image(
        self, *, file_key, file_url, content_type, size, uploader_id, db
    ):
        self.reserved.append(file_key)
        return SimpleNamespace(id=uuid.uuid4())

    async def lock_reserved_image(self, image_id, *, db):
        self.locked.append(image_id)
        return None if self.reclaimed else SimpleNamespace(id=image_id)

    async def confirm_reserved_image(self, image_id, *, size, content_type, db):
        self.confirmed.append(image_id)
        return SimpleNamespace(id=image_id, file_url="https://cdn/x.png")


def _patch_repo(monkeypatch, **kw) -> _ReservingRepo:
    repo = _ReservingRepo(**kw)
    monkeypatch.setattr(media_service_mod, "MediaRepository", repo)
    monkeypatch.setattr(media_service_mod, "build_url", lambda k: f"https://cdn/{k}")
    return repo


async def _upload(expected_size: int | None):
    """예약 → 승격 → 확정 한 바퀴. 블록 안에서 할 일이 없는 일반 업로드와 같다."""
    async with MediaService._reserved_upload(
        _valid_pending_key(),
        purpose="post",
        expected_size=expected_size,
        uploader_id=None,
        db=as_session(FakeDB()),
    ) as upload:
        pass
    return upload


async def test_confirm_rejects_size_mismatch_before_promote(monkeypatch):
    async def fake_head(key):
        return _meta(size=100)

    async def fail_promote(key, dest_key, *, etag):
        raise AssertionError("검증 실패 시 promote(영구 copy + pending 삭제)가 실행되면 안 된다")

    monkeypatch.setattr(media_service_mod, "head_pending_object", fake_head)
    monkeypatch.setattr(media_service_mod, "promote_pending_object", fail_promote)

    with pytest.raises(InvalidImageFileException):
        await _upload(99)


async def test_confirm_rejects_disallowed_type_before_promote(monkeypatch):
    """storage 계층은 webp를 승격할 수 있지만 정책(ALLOWED_IMAGE_TYPES)이 거부한다 —
    이 거부가 promote 앞이어야 영구 객체가 안 남는다."""
    from app.common.exceptions import InvalidFileTypeException
    from app.core.config import settings

    monkeypatch.setattr(settings, "ALLOWED_IMAGE_TYPES", ["image/jpeg", "image/png"])

    async def fake_head(key):
        return _meta(content_type="image/webp")

    async def fail_promote(key, dest_key, *, etag):
        raise AssertionError("정책 거부 대상이 promote되면 안 된다")

    monkeypatch.setattr(media_service_mod, "head_pending_object", fake_head)
    monkeypatch.setattr(media_service_mod, "promote_pending_object", fail_promote)

    with pytest.raises(InvalidFileTypeException):
        await _upload(100)


async def test_confirm_maps_missing_object_to_400(monkeypatch):
    """미업로드·이미 소진(승격)된 1회성 키 → head의 ValueError가 400 계열로 매핑된다(500 금지)."""

    async def fake_head(key):
        raise ValueError("pending object not found")

    monkeypatch.setattr(media_service_mod, "head_pending_object", fake_head)

    with pytest.raises(InvalidImageFileException):
        await _upload(None)


async def test_confirm_leaves_reserved_row_when_source_changed_since_validation(monkeypatch):
    """head~copy 사이 재업로드로 선검증을 우회하려 한 경우 — copy가 ETag 불일치로 실패하고,
    예약 행은 **지우지 않는다.**

    보상 삭제는 그 자체로 실패할 수 있고, 실패하면 행 없는 객체가 영구히 남는다. 대신 예약
    행을 그대로 둬 스위퍼가 회수하게 한다(ADR 0019). 사용자에겐 400.
    """
    repo = _patch_repo(monkeypatch)

    async def fake_head(key):
        return _meta(size=100, content_type="image/png")

    async def fake_promote(key, dest_key, *, etag):
        raise ValueError("pending object missing or changed since validation")

    def boom(_key):
        raise AssertionError("보상 삭제를 쓰면 실패 시 회수 불가능한 객체가 남는다")

    monkeypatch.setattr(media_service_mod, "head_pending_object", fake_head)
    monkeypatch.setattr(media_service_mod, "promote_pending_object", fake_promote)
    monkeypatch.setattr(media_service_mod, "storage_delete", boom)

    with pytest.raises(InvalidImageFileException):
        await _upload(100)

    assert repo.reserved, "승격 전에 예약 행이 없으면 회수할 근거가 없다"
    assert repo.confirmed == [], "승격 실패분이 확정되면 안 된다"


async def test_upload_reserves_then_locks_then_promotes_with_validated_etag(monkeypatch):
    """정상 경로 — 예약 행 커밋 → 그 행을 잠금 → 첫 HEAD의 ETag를 조건으로 승격 → 확정."""
    order: list[str] = []
    repo = _patch_repo(monkeypatch)

    async def fake_head(key):
        return _meta(size=100, content_type="image/png")

    async def fake_promote(key, dest_key, *, etag):
        order.append("promote")
        assert repo.reserved == [dest_key], "행 없이 승격하면 그 사이 실패가 영구 누수가 된다"
        assert repo.locked, "잠그지 않고 승격하면 스위퍼가 그 사이 행을 가져갈 수 있다"
        assert etag == '"etag-1"', "검증한 객체의 ETag를 copy 조건으로 넘겨야 재업로드를 막는다"

    monkeypatch.setattr(media_service_mod, "head_pending_object", fake_head)
    monkeypatch.setattr(media_service_mod, "promote_pending_object", fake_promote)

    upload = await _upload(100)

    assert order == ["promote"]
    assert repo.reserved[0].startswith("post/") and repo.reserved[0].endswith(".png")
    assert repo.confirmed == [upload.image.id]


async def test_upload_fails_before_promote_when_sweeper_already_reclaimed(monkeypatch):
    """잠그려는데 스위퍼가 먼저 가져갔으면 **승격 전에** 실패한다 — 객체가 안 만들어진다."""
    from app.common.exceptions import InternalServerErrorException

    _patch_repo(monkeypatch, reclaimed=True)

    async def fake_head(key):
        return _meta()

    async def fake_promote(key, dest_key, *, etag):
        raise AssertionError("회수된 행에 승격하면 행 없는 객체가 생긴다")

    monkeypatch.setattr(media_service_mod, "head_pending_object", fake_head)
    monkeypatch.setattr(media_service_mod, "promote_pending_object", fake_promote)

    with pytest.raises(InternalServerErrorException):
        await _upload(100)


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


# --- 삭제 경로: 요청 안에서는 DB만 건드린다 ---


def _patch_delete_repo(monkeypatch, *, owned: bool) -> list[Any]:
    """`delete_image`가 보는 리포지터리·스토리지를 갈아끼우고 소프트 삭제된 id 싱크를 돌려준다.

    스토리지는 호출되면 터지게 둔다 — 요청 경로가 S3를 건드리지 않는다는 것이 이 계약의
    핵심이라, 조용히 통과시키면 회귀를 못 잡는다.
    """
    soft_deleted: list[Any] = []

    class _Repo:
        @staticmethod
        async def soft_delete_image_if_owned(iid, uid, *, db):
            if not owned:
                return False
            soft_deleted.append(iid)
            return True

    def _boom(_key):
        raise AssertionError("요청 경로에서 스토리지를 건드렸다")

    monkeypatch.setattr(media_service_mod, "MediaRepository", _Repo)
    monkeypatch.setattr(media_service_mod, "storage_delete", _boom)
    return soft_deleted


async def test_delete_image_never_touches_storage_in_request_path(monkeypatch):
    """삭제 요청은 DB 쓰기 하나로 끝난다 — S3가 죽어 있어도 사용자는 성공을 받는다.

    S3와 DB는 한 트랜잭션으로 묶을 수 없어, 요청 안에서 둘 다 건드리면 "앞은 됐는데 뒤가
    실패"가 반드시 존재한다(backlog #44). 스토리지 삭제는 스위퍼가 뒤에서 재시도하며 맡는다.
    """
    image_id, user_id = uuid.uuid4(), uuid.uuid4()
    soft_deleted = _patch_delete_repo(monkeypatch, owned=True)

    await MediaService.delete_image(image_id, user_id, as_session(FakeDB()))

    assert soft_deleted == [image_id]


async def test_delete_image_raises_not_found_when_not_owned(monkeypatch):
    """없거나·남의 것이거나·이미 지운 것은 전부 404 — 셋을 구분해 알리면 존재가 노출된다."""
    from app.common.exceptions import ImageNotFoundException

    _patch_delete_repo(monkeypatch, owned=False)

    with pytest.raises(ImageNotFoundException):
        await MediaService.delete_image(uuid.uuid4(), uuid.uuid4(), as_session(FakeDB()))


async def test_signup_confirms_only_after_token_is_issued(monkeypatch):
    """가입 확정은 토큰이 발급된 **뒤에** 일어난다 — 그래서 되돌릴 것이 없다.

    순서가 반대면(확정 먼저, 토큰 나중) 토큰 발급 실패 시 확정을 되돌리는 쓰기가 필요하다.
    그건 이 브랜치가 S3에서 없앤 보상 쓰기가 DB로 되살아난 것일 뿐이고, 그 되돌리기 역시
    실패할 수 있다. 확정을 맨 뒤로 미루면 실패는 예약 행을 남기고 스위퍼가 회수한다.
    """
    from app.domain.media.schema import ConfirmSignupUploadRequest

    repo = _patch_repo(monkeypatch)

    async def fake_head(key):
        return _meta()

    async def fake_promote(key, dest_key, *, etag):
        return None

    async def fail_token(image_id_, redis):
        assert repo.confirmed == [], "토큰보다 먼저 확정하면 되돌리는 쓰기가 필요해진다"
        raise RuntimeError("redis down")

    def boom(_key):
        raise AssertionError("가입 확정 실패가 스토리지를 건드리면 안 된다")

    monkeypatch.setattr(media_service_mod, "head_pending_object", fake_head)
    monkeypatch.setattr(media_service_mod, "promote_pending_object", fake_promote)
    monkeypatch.setattr(MediaService, "issue_upload_token", fail_token)
    monkeypatch.setattr(media_service_mod, "storage_delete", boom)

    with pytest.raises(RuntimeError):
        await MediaService.confirm_presigned_signup_upload(
            ConfirmSignupUploadRequest(file_key=_valid_pending_key(), size=100),
            as_session(FakeDB()),
            None,
        )

    assert repo.confirmed == [], "실패했는데 확정돼 있으면 되돌릴 쓰기가 필요하다"
