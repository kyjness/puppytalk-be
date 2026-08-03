# 전역 예외 핸들러. 모든 에러 응답을 ApiResponse와 동일한 바디(code, message, data, requestId)로 통일.
# 500 시 클라이언트에는 스택/쿼리 노출 금지. 서버 로그는 에러 시에만 구조화(JSON 한 줄) + 필요 시 traceback.
import json
import logging
from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DatabaseError, IntegrityError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.common import ApiCode
from app.common.codes import UNIQUE_CONSTRAINT_CODES
from app.common.exceptions import BaseProjectException
from app.common.responses import error_body, get_request_id

logger = logging.getLogger(__name__)

MASKED_500_MESSAGE = "Internal Server Error"


def _error_payload(
    code: str,
    message: str = "",
    data: object | None = None,
    *,
    request: Request,
) -> dict[str, Any]:
    return error_body(code, message, data, request_id=get_request_id(request))


def _log_error_structured(
    request: Request,
    event: str,
    exc: BaseException | None = None,
    *,
    level: int = logging.ERROR,
    **fields: Any,
) -> None:
    payload: dict[str, Any] = {
        "event": event,
        "request_id": get_request_id(request),
        "path": request.url.path,
        "method": request.method,
        **fields,
    }
    if exc is not None:
        payload["exc_type"] = type(exc).__name__
        payload["exc_msg"] = str(exc)[:2000]
    line = json.dumps(payload, ensure_ascii=False)
    if exc is not None:
        logger.exception("%s", line)
    else:
        logger.log(level, "%s", line)


def _masked_500_response(
    request: Request, exc: Exception, *, event: str, code: ApiCode, status_code: int = 500
) -> JSONResponse:
    """5xx 응답의 단일 조립처: 구조화 로그 + 메시지 마스킹 + data 미노출.

    모든 5xx 경로가 이 헬퍼를 지나야 마스킹 정책(메시지·data 모두)이 반쪽 적용될 수 없다."""
    _log_error_structured(request, event, exc)
    return JSONResponse(
        status_code=status_code,
        content=_error_payload(code.value, MASKED_500_MESSAGE, None, request=request),
    )


HTTP_STATUS_TO_CODE = {
    400: ApiCode.INVALID_REQUEST,
    401: ApiCode.UNAUTHORIZED,
    403: ApiCode.FORBIDDEN,
    404: ApiCode.NOT_FOUND,
    405: ApiCode.METHOD_NOT_ALLOWED,
    409: ApiCode.CONFLICT,
    422: ApiCode.UNPROCESSABLE_ENTITY,
    429: ApiCode.RATE_LIMIT_EXCEEDED,
    500: ApiCode.INTERNAL_SERVER_ERROR,
}


def register_exception_handlers(app: FastAPI) -> None:
    def _pick_validation_error(errors: Sequence[Any]) -> tuple[str, str]:
        """(code, message)를 **같은 에러**에서 뽑는다 — 코드는 에러 N, 메시지는 에러 0을
        쓰면 짝이 어긋난 응답이 된다.

        검증기는 ValueError(ApiCode.X.name)로 실패를 알린다 → pydantic msg는
        "Value error, X". 접두 제거 후 ApiCode 이름과 **정확 일치**로 해석하므로
        부분 문자열 충돌(INVALID_REQUEST ⊂ INVALID_REQUEST_BODY)도, 코드별 매핑
        테이블을 따로 관리할 필요도 없다. 매칭 없으면 (INVALID_REQUEST_BODY, 첫 msg)."""
        first_msg = ""
        for err in errors:
            msg = err.get("msg") if isinstance(err, dict) else None
            if not isinstance(msg, str):
                continue
            first_msg = first_msg or msg
            code = ApiCode.__members__.get(msg.removeprefix("Value error, "))
            if code is not None:
                return code.value, msg
        return ApiCode.INVALID_REQUEST_BODY.value, first_msg

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        code, message = _pick_validation_error(exc.errors())
        return JSONResponse(
            status_code=400,
            content=_error_payload(code, message, None, request=request),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        # 앱 코드는 BaseProjectException만 raise한다 — 여기는 프레임워크발 전용.
        # 핸들러 조회는 예외의 MRO를 따르므로 Starlette 기반 클래스로 등록해야
        # 라우팅 404/405(Starlette가 직접 raise)와 FastAPI HTTPException을 모두 받는다.
        headers = dict(exc.headers) if exc.headers else {}
        code_str = (HTTP_STATUS_TO_CODE.get(exc.status_code) or ApiCode.HTTP_ERROR).value
        message = exc.detail if isinstance(exc.detail, str) else ""
        content = _error_payload(code_str, message, None, request=request)
        return JSONResponse(status_code=exc.status_code, content=content, headers=headers)

    @app.exception_handler(IntegrityError)
    async def integrity_error_handler(request: Request, exc: IntegrityError):
        # PostgreSQL 전용 매핑: SQLSTATE(23505 unique·23503 FK) + psycopg diag의 제약명.
        # psycopg v3 예외는 pgcode가 아니라 sqlstate 속성을 노출한다(pgcode는 v2 잔재 —
        # 그걸 읽으면 매핑 전체가 프로덕션에서 죽는다). 에러 메시지 문자열 파싱은
        # 로케일·드라이버 포맷에 취약해 쓰지 않는다.
        orig = getattr(exc, "orig", None)
        sqlstate = getattr(orig, "sqlstate", None) if orig else None
        diag = getattr(orig, "diag", None) if orig else None
        constraint = (getattr(diag, "constraint_name", None) or "").lower()
        if sqlstate == "23505":
            _log_error_structured(
                request, "db_integrity_duplicate", level=logging.WARNING, constraint=constraint
            )
            code = next(
                (c for frag, c in UNIQUE_CONSTRAINT_CODES if frag in constraint),
                ApiCode.CONFLICT,
            )
            return JSONResponse(
                status_code=409,
                content=_error_payload(code.value, "", None, request=request),
            )
        if sqlstate == "23503":
            _log_error_structured(request, "db_integrity_fk", exc, constraint=constraint)
            return JSONResponse(
                status_code=409,
                content=_error_payload(ApiCode.CONSTRAINT_ERROR.value, "", None, request=request),
            )
        _log_error_structured(request, "db_integrity_other", exc, sqlstate=sqlstate)
        return JSONResponse(
            status_code=400,
            content=_error_payload(ApiCode.INVALID_REQUEST.value, "", None, request=request),
        )

    @app.exception_handler(DatabaseError)
    async def database_error_handler(request: Request, exc: DatabaseError):
        # OperationalError는 DatabaseError의 하위 클래스 — 핸들러 조회가 MRO를 따르므로
        # 여기 하나로 잡고 로그 event 라벨만 구분한다(응답은 동일).
        event = "db_operational_error" if isinstance(exc, OperationalError) else "db_database_error"
        return _masked_500_response(request, exc, event=event, code=ApiCode.DB_ERROR)

    @app.exception_handler(BaseProjectException)
    async def project_exception_handler(request: Request, exc: BaseProjectException):
        # 5xx는 다른 500 경로(DatabaseError·unhandled)와 동일 정책(로그+마스킹, data 미노출).
        # 4xx는 기대된 흐름이라 로그 없음.
        if exc.status_code >= 500:
            return _masked_500_response(
                request,
                exc,
                event="project_exception_5xx",
                code=exc.code,
                status_code=exc.status_code,
            )
        content = _error_payload(exc.code.value, exc.message or "", exc.data, request=request)
        return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)

    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception):
        return _masked_500_response(
            request, exc, event="unhandled_exception", code=ApiCode.INTERNAL_SERVER_ERROR
        )
