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
