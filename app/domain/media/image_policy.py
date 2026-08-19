# 이미지 업로드 정책(presigned 경로). purpose·Content-Type 검증, presign용 파일명 정규화.
# 커스텀 예외 사용 → 전역 handler가 400 응답 처리.
import os
import re
from uuid import UUID

from app.common.exceptions import (
    InvalidFileTypeException,
    InvalidImageFileException,
)
from app.core.config import settings
from app.core.ids import new_ulid_str
from app.infra.storage import PENDING_KEY_PREFIX

CONTENT_TYPE_EXT: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


def validate_image_content_type(content_type: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct not in settings.ALLOWED_IMAGE_TYPES or ct not in CONTENT_TYPE_EXT:
        raise InvalidFileTypeException()
    return ct


def sanitize_presign_filename(filename: str, content_type: str) -> str:
    """Presigned POST용 안전 파일명. 확장자는 Content-Type과 일치하도록 강제."""
    ct = validate_image_content_type(content_type)
    ext = CONTENT_TYPE_EXT[ct]
    base = os.path.basename((filename or "").strip())
    if not base or base in (".", ".."):
        raise InvalidImageFileException()
    stem = re.sub(r"[^\w.\-]", "_", base.rsplit(".", 1)[0])[:100] or "upload"
    return f"{stem}.{ext}"


def build_pending_file_key(upload_id: UUID, filename: str) -> str:
    return f"{PENDING_KEY_PREFIX}{upload_id}/{filename}"


def build_permanent_file_key(purpose: str, content_type: str) -> str:
    """승격 목적지 키. 확장자 정책이 여기 있으므로 어댑터가 아니라 도메인이 만든다.

    호출부가 승격 **전에** 키를 알아야 하기 때문에 어댑터에서 올라왔다 — DB 행을 먼저 만들고
    S3로 승격해야 "행 없는 객체"가 생기지 않는다(ADR 0019).
    """
    validated = validate_image_content_type(content_type)
    return f"{purpose}/{new_ulid_str()}.{CONTENT_TYPE_EXT[validated]}"
