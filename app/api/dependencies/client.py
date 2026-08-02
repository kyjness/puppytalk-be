# 요청 기반 클라이언트 식별자 + POST /posts 멱등성(X-Idempotency-Key, ADR 0008).
# 멱등 소비자는 게시글 생성 하나라 다중 네임스페이스 매개변수화 없이 post:create 전용으로 둔다 —
# 두 번째 소비자가 생기면 그때 네임스페이스·어댑터를 인자로 끌어올린다.
import hashlib
import json
import logging
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError

from app.common.codes import ApiCode
from app.common.responses import get_request_id
from app.common.schemas import ApiResponse
from app.core.config import settings
from app.core.middleware.proxy_headers import client_ip_from_scope
from app.domain.posts.schemas import PostIdData
from app.infra.redis import bulk_to_str, get_app_redis

log = logging.getLogger(__name__)

_IDEMP_NAMESPACE = "post:create"
_IDEMP_ADAPTER = TypeAdapter(ApiResponse[PostIdData])
_IDEMP_SUCCESS_STATUS = 201
_IDEMP_CONFLICT_MESSAGE = "동일 멱등성 키로 게시글 생성이 진행 중입니다."

_IDEMP_KEY_MIN = 8
_IDEMP_KEY_MAX = 128


def get_client_identifier(request: Request) -> str:
    """조회수 dedup(viewer_key)용 클라이언트 식별자 — rate limit 키 산정과 동일하게
    프록시 검증이 끝난 scope["client"]만 쓴다(client_ip_from_scope가 규약을 소유)."""
    return client_ip_from_scope(request.scope, default="0.0.0.0").strip()


def _normalize_idempotency_key(raw: str | None) -> str | None:
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    if len(s) < _IDEMP_KEY_MIN or len(s) > _IDEMP_KEY_MAX:
        raise HTTPException(
            status_code=400,
            detail={
                "code": ApiCode.INVALID_REQUEST.value,
                "message": f"X-Idempotency-Key는 {_IDEMP_KEY_MIN}~{_IDEMP_KEY_MAX}자여야 합니다.",
                "data": None,
            },
        )
    return s


def _idempotency_fingerprint(user_id: UUID, norm: str) -> str:
    # 유저 스코프 fingerprint: 다른 사용자의 같은 키와 충돌·열람되지 않는다(ADR 0008).
    return hashlib.sha256(f"{user_id}:{norm}".encode()).hexdigest()


def _result_redis_key(fp: str) -> str:
    return f"idemp:{_IDEMP_NAMESPACE}:res:{fp}"


def _lock_redis_key(fp: str) -> str:
    return f"idemp:{_IDEMP_NAMESPACE}:lock:{fp}"


def _merge_request_id_into_cached_body(body: dict[str, Any], request: Request) -> dict[str, Any]:
    out = dict(body)
    out["requestId"] = get_request_id(request)
    return out


async def post_create_idempotency_before(
    request: Request, user_id: UUID, raw_key: str | None
) -> tuple[JSONResponse | None, str | None]:
    """결과 캐시 히트면 저장된 성공 응답 재생(requestId만 갱신), 미스면 in-flight 락 선점.

    같은 키가 처리 중이면 409, Redis 오류는 멱등성 없이 진행(fail-open, ADR 0005).
    반환 (캐시된 응답, fingerprint) — after 훅은 이 fingerprint를 그대로 받는다.
    훅마다 raw 헤더를 재검증·재해시하면 검증 규칙이 두 벌로 드리프트해, before는 락을
    잡았는데 after가 키를 무효 판정해 해제를 건너뛰는(락 TTL 동안 재시도 전부 409)
    표면이 생긴다 — 검증·해시는 여기 한 번뿐이다."""
    norm = _normalize_idempotency_key(raw_key)
    if norm is None:
        return None, None

    fp = _idempotency_fingerprint(user_id, norm)
    rcli = get_app_redis(request.app)
    if rcli is None:
        return None, fp

    try:
        payload = bulk_to_str(await rcli.get(_result_redis_key(fp)))
        if payload:
            try:
                validated = _IDEMP_ADAPTER.validate_json(payload)
            except ValidationError as e:
                log.warning(
                    "멱등성 캐시 검증 실패(캐시 미스 처리) fp_prefix=%s: %s",
                    fp[:16],
                    e,
                )
            else:
                body = validated.model_dump(mode="json", by_alias=True)
                return JSONResponse(
                    status_code=_IDEMP_SUCCESS_STATUS,
                    content=_merge_request_id_into_cached_body(body, request),
                ), fp

        got_lock = await rcli.set(
            _lock_redis_key(fp),
            "1",
            nx=True,
            ex=settings.IDEMPOTENCY_POST_CREATE_LOCK_TTL_SECONDS,
        )
        if not got_lock:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": ApiCode.CONFLICT.value,
                    "message": _IDEMP_CONFLICT_MESSAGE,
                    "data": None,
                },
            )
    except HTTPException:
        raise
    except Exception as e:
        log.warning("멱등성 Redis 오류(Fail-open): %s", e)
        return None, fp

    return None, fp


async def post_create_idempotency_after_success(
    request: Request,
    fp: str | None,
    response_obj: Any,
) -> None:
    if fp is None:
        return

    rcli = get_app_redis(request.app)
    if rcli is None:
        return

    try:
        dumped = response_obj.model_dump(mode="json", by_alias=True)
        await rcli.set(
            _result_redis_key(fp),
            json.dumps(dumped, ensure_ascii=False),
            ex=settings.IDEMPOTENCY_POST_CREATE_TTL_SECONDS,
        )
    except Exception as e:
        log.warning("멱등성 성공 캐시 저장 실패: %s", e)
    try:
        await rcli.delete(_lock_redis_key(fp))
    except Exception as e:
        log.warning("멱등성 잠금 해제 실패: %s", e)


async def post_create_idempotency_after_failure(
    request: Request,
    fp: str | None,
) -> None:
    if fp is None:
        return

    rcli = get_app_redis(request.app)
    if rcli is None:
        return

    try:
        await rcli.delete(_lock_redis_key(fp))
    except Exception as e:
        log.warning("멱등성 실패 시 잠금 해제 오류: %s", e)
