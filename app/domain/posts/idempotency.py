# POST /posts 멱등성(X-Idempotency-Key, ADR 0008). 소비자가 게시글 생성 하나라
# 다중 네임스페이스 매개변수화 없이 post:create 전용으로 둔다 — 두 번째 소비자가 생기면
# 그때 네임스페이스·어댑터를 인자로 끌어올린다.
import asyncio
import hashlib
import json
import logging
from typing import Any, NamedTuple
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError

from app.common.exceptions import ConcurrentUpdateException, InvalidRequestException
from app.common.responses import get_request_id
from app.common.schemas import ApiResponse
from app.core.config import settings
from app.domain.posts.schemas import PostIdData
from app.infra.lock import new_lock_token, release_lock, try_acquire_lock
from app.infra.redis import RedisLike, bulk_to_str, get_app_redis

log = logging.getLogger(__name__)

_IDEMP_NAMESPACE = "post:create"
_IDEMP_ADAPTER = TypeAdapter(ApiResponse[PostIdData])
_IDEMP_SUCCESS_STATUS = 201
_IDEMP_CONFLICT_MESSAGE = "동일 멱등성 키로 게시글 생성이 진행 중입니다."

_IDEMP_KEY_MIN = 8
_IDEMP_KEY_MAX = 128


class IdempotencyHandle(NamedTuple):
    """before 훅이 after 훅에 넘기는 상관 정보.

    검증·해시·락 획득이 before 한 곳에서만 일어나도록 결과를 통째로 실어 나른다.
    `lock_token`은 **락을 잡았거나 잡았을 수도 있을 때** 채워진다 — 획득 명령이 예외로
    끝났어도(응답만 늦은 타임아웃) 서버에는 잡혔을 수 있으므로 토큰을 버리지 않는다. 해제는
    CAS라 내 토큰일 때만 지우니 "아마도 잡힘"을 풀어도 남의 락은 건드리지 않는다.
    Redis 부재·캐시 히트처럼 획득을 **시도조차 안 한** 경우에만 None이다.
    """

    fingerprint: str
    lock_token: str | None


def _normalize_idempotency_key(raw: str | None) -> str | None:
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    if len(s) < _IDEMP_KEY_MIN or len(s) > _IDEMP_KEY_MAX:
        raise InvalidRequestException(
            f"X-Idempotency-Key는 {_IDEMP_KEY_MIN}~{_IDEMP_KEY_MAX}자여야 합니다."
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
) -> tuple[JSONResponse | None, IdempotencyHandle | None]:
    """결과 캐시 히트면 저장된 성공 응답 재생(requestId만 갱신), 미스면 in-flight 락 선점.

    같은 키가 처리 중이면 409, Redis 오류는 멱등성 없이 진행(fail-open, ADR 0005).
    반환 (캐시된 응답, 핸들) — after 훅은 이 핸들을 그대로 받는다.
    훅마다 raw 헤더를 재검증·재해시하면 검증 규칙이 두 벌로 드리프트해, before는 락을
    잡았는데 after가 키를 무효 판정해 해제를 건너뛰는(락 TTL 동안 재시도 전부 409)
    표면이 생긴다 — 검증·해시는 여기 한 번뿐이다."""
    norm = _normalize_idempotency_key(raw_key)
    if norm is None:
        return None, None

    fp = _idempotency_fingerprint(user_id, norm)
    rcli = get_app_redis(request.app)
    if rcli is None:
        return None, IdempotencyHandle(fp, None)

    # 토큰은 try 밖에서 만든다 — SET이 서버에 적용됐는데 응답만 늦어 예외가 나면, 이 토큰으로
    # after 훅이 CAS 해제를 시도한다. 안 그러면 TTL 동안 같은 키의 재시도가 전부 409다.
    token = new_lock_token()
    # fail-open try는 Redis I/O만 감싼다 — 의도된 409는 try 밖에서 던져 삼켜질 표면 자체를 없앤다.
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
                ), IdempotencyHandle(fp, None)

        # 공용 락 프리미티브(app/infra/lock.py) — 랜덤 토큰 발급 + CAS 해제. 직접 SET NX 하고
        # DEL 로 풀면 락 TTL이 만료된 뒤 뒤늦게 끝난 요청이 **다음 요청의 락**을 지운다.
        acquired = await try_acquire_lock(
            rcli,
            _lock_redis_key(fp),
            settings.IDEMPOTENCY_POST_CREATE_LOCK_TTL_SECONDS,
            token=token,
        )
    except Exception as e:
        log.warning("멱등성 Redis 오류(Fail-open): %s", e)
        return None, IdempotencyHandle(fp, token)  # "아마도 잡힘" — 해제는 CAS라 안전

    if acquired is None:
        raise ConcurrentUpdateException(_IDEMP_CONFLICT_MESSAGE)
    return None, IdempotencyHandle(fp, token)


async def _store_success_result(rcli: RedisLike, fp: str, response_obj: Any) -> None:
    try:
        dumped = response_obj.model_dump(mode="json", by_alias=True)
        await rcli.set(
            _result_redis_key(fp),
            json.dumps(dumped, ensure_ascii=False),
            ex=settings.IDEMPOTENCY_POST_CREATE_TTL_SECONDS,
        )
    except Exception as e:
        log.warning("멱등성 성공 캐시 저장 실패: %s", e)


async def _release_if_held(rcli: RedisLike, handle: IdempotencyHandle) -> None:
    """내가 잡은(또는 잡았을 수 있는) 락만 CAS로 해제. 토큰이 없으면 시도조차 안 한 것이다."""
    if handle.lock_token is None:
        return
    await release_lock(rcli, _lock_redis_key(handle.fingerprint), handle.lock_token)


async def post_create_idempotency_after_success(
    request: Request,
    handle: IdempotencyHandle | None,
    response_obj: Any,
) -> None:
    if handle is None:
        return

    rcli = get_app_redis(request.app)
    if rcli is None:
        return

    # 결과 저장과 락 해제는 서로 의존이 없고 각자 실패를 삼킨다 — 병렬로(지연이 합이 아니라 max).
    await asyncio.gather(
        _store_success_result(rcli, handle.fingerprint, response_obj),
        _release_if_held(rcli, handle),
    )


async def post_create_idempotency_after_failure(
    request: Request,
    handle: IdempotencyHandle | None,
) -> None:
    if handle is None:
        return

    rcli = get_app_redis(request.app)
    if rcli is None:
        return

    await _release_if_held(rcli, handle)
