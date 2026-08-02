# S3 파일 스토리지(단일 경로). dev/CI는 S3 호환 MinIO(엔드포인트만 다름), prod는 실제 S3.

import re
from collections.abc import Mapping
from typing import Any

from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.ids import new_ulid_str

_s3_client = None

_MEDIA_PREFIX = "media/"
PENDING_KEY_PREFIX = "pending/"
PRESIGNED_MAX_BYTES = 10 * 1024 * 1024
PRESIGNED_POST_EXPIRES_SECONDS = 900
_PENDING_KEY_RE = re.compile(
    r"^pending/[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/[\w.\-]+$"
)


def _strip_redundant_media_prefixes(key: str) -> str:
    """DB/호출부에 media/ 가 중복·혼입돼도 S3 키·공개 URL에서 한 번만 쓰이도록 본문 경로만 남김."""
    k = (key or "").strip().lstrip("/")
    while k.startswith(_MEDIA_PREFIX):
        k = k[len(_MEDIA_PREFIX) :].lstrip("/")
    return k


def _s3_object_key(key: str) -> str:
    body = _strip_redundant_media_prefixes(key)
    if not body:
        raise ValueError("storage key must not be empty")
    return f"{_MEDIA_PREFIX}{body}"


def _get_s3_client():
    """S3 클라이언트 Lazy-loading. 인증 정보 누락 시 ValueError."""
    global _s3_client
    if _s3_client is not None:
        return _s3_client
    if (
        not settings.S3_BUCKET_NAME
        or not settings.AWS_ACCESS_KEY_ID
        or not settings.AWS_SECRET_ACCESS_KEY
    ):
        raise ValueError(
            "S3_BUCKET_NAME, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY must be set for storage"
        )
    import boto3
    from botocore.config import Config

    kwargs: dict = {
        "region_name": settings.AWS_REGION,
        "aws_access_key_id": settings.AWS_ACCESS_KEY_ID,
        "aws_secret_access_key": settings.AWS_SECRET_ACCESS_KEY,
    }
    if settings.S3_ENDPOINT_URL:
        # 커스텀 엔드포인트(MinIO 등)는 path-style이라야 버킷을 호스트가 아닌 경로로 접근한다
        # (virtual-hosted 'bucket.host'는 DNS 미해석). 실제 S3는 무해.
        kwargs["endpoint_url"] = settings.S3_ENDPOINT_URL
        kwargs["config"] = Config(s3={"addressing_style": "path"})
    _s3_client = boto3.client("s3", **kwargs)
    return _s3_client


def storage_delete(key: str) -> None:
    client = _get_s3_client()
    client.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=_s3_object_key(key))


def build_url(key: str) -> str:
    path_under_media = _strip_redundant_media_prefixes(key)
    if settings.S3_PUBLIC_BASE_URL:
        base = settings.S3_PUBLIC_BASE_URL.rstrip("/")
        return f"{base}/{path_under_media}"
    return (
        f"https://{settings.S3_BUCKET_NAME}.s3.{settings.AWS_REGION}.amazonaws.com/"
        f"{_s3_object_key(key)}"
    )


def is_valid_pending_file_key(file_key: str) -> bool:
    k = (file_key or "").strip().lstrip("/")
    return bool(_PENDING_KEY_RE.fullmatch(k))


def _generate_presigned_post_sync(file_key: str, content_type: str) -> dict[str, Any]:
    client = _get_s3_client()
    s3_key = _s3_object_key(file_key)
    return client.generate_presigned_post(
        Bucket=settings.S3_BUCKET_NAME,
        Key=s3_key,
        Fields={"Content-Type": content_type},
        Conditions=[
            {"Content-Type": content_type},
            ["content-length-range", 1, PRESIGNED_MAX_BYTES],
        ],
        ExpiresIn=PRESIGNED_POST_EXPIRES_SECONDS,
    )


async def issue_presigned_post(
    file_key: str,
    content_type: str,
) -> tuple[str, dict[str, str]]:
    """Presigned POST(url, fields) 발급. boto3 동기 호출은 스레드풀로 오프로딩."""
    payload = await run_in_threadpool(_generate_presigned_post_sync, file_key, content_type)
    fields = {str(k): str(v) for k, v in payload["fields"].items()}
    return str(payload["url"]), fields


def _head_pending_object_sync(file_key: str) -> dict[str, Any]:
    if not is_valid_pending_file_key(file_key):
        raise ValueError("invalid pending file_key")
    from botocore.exceptions import ClientError

    client = _get_s3_client()
    try:
        return client.head_object(Bucket=settings.S3_BUCKET_NAME, Key=_s3_object_key(file_key))
    except ClientError as e:
        # 미업로드·이미 소진(승격)된 1회성 키의 404는 클라이언트 입력 오류 신호(→400 매핑) —
        # 그 외 ClientError(권한·엔드포인트 장애)는 5xx로 남긴다.
        status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status == 404:
            raise ValueError("pending object not found") from e
        raise


def _promote_pending_object_sync(
    pending_key: str,
    dest_purpose: str,
    ext_by_content_type: Mapping[str, str],
) -> tuple[str, int, str]:
    """pending/ 객체를 영구 purpose 경로로 copy 후 삭제. 키 검증은 head가 담당한다.

    purpose·Content-Type 허용 정책은 도메인(media image_policy) 소유 — 어댑터는
    전달받은 확장자 매핑에 없는 타입만 거부한다.
    """
    meta = _head_pending_object_sync(pending_key)
    size = int(meta.get("ContentLength") or 0)
    if size < 1 or size > PRESIGNED_MAX_BYTES:
        raise ValueError("object size out of allowed range")
    content_type = str(meta.get("ContentType") or "")
    if not content_type:
        raise ValueError("missing content type")

    ext = ext_by_content_type.get(content_type.split(";")[0].strip().lower())
    if not ext:
        raise ValueError("unsupported content type")
    dest_key = f"{dest_purpose}/{new_ulid_str()}.{ext}"
    bucket = settings.S3_BUCKET_NAME
    client = _get_s3_client()
    client.copy_object(
        Bucket=bucket,
        Key=_s3_object_key(dest_key),
        CopySource={"Bucket": bucket, "Key": _s3_object_key(pending_key)},
        ContentType=content_type,
        MetadataDirective="REPLACE",
    )
    client.delete_object(Bucket=bucket, Key=_s3_object_key(pending_key))
    return dest_key, size, content_type


async def head_pending_object(file_key: str) -> dict[str, Any]:
    return await run_in_threadpool(_head_pending_object_sync, file_key)


async def promote_pending_object(
    pending_key: str,
    dest_purpose: str,
    *,
    ext_by_content_type: Mapping[str, str],
) -> tuple[str, int, str]:
    return await run_in_threadpool(
        _promote_pending_object_sync, pending_key, dest_purpose, ext_by_content_type
    )
