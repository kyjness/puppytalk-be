# 전역 예외 핸들러. 모든 에러 응답을 ApiResponse와 동일한 바디(code, message, data, requestId)로 통일.
# 500 시 클라이언트에는 스택/쿼리 노출 금지. 서버 로그는 에러 시에만 구조화(JSON 한 줄) + 필요 시 traceback.
import json
import logging
from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DatabaseError, IntegrityError, OperationalError

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
    _VALIDATION_CODE_NAMES = frozenset(
        {
            ApiCode.INVALID_REQUEST_BODY.name,
            ApiCode.INVALID_REQUEST.name,
            ApiCode.INVALID_PASSWORD_FORMAT.name,
            ApiCode.INVALID_FILE_FORMAT.name,
            ApiCode.MISSING_REQUIRED_FIELD.name,
            ApiCode.POST_FILE_LIMIT_EXCEEDED.name,
        }
    )

    def _pick_validation_code(errors: Sequence[Any]) -> str:
        for err in errors:
            msg = err.get("msg", "") if isinstance(err.get("msg"), str) else ""
            for name in _VALIDATION_CODE_NAMES:
                if name in msg or msg == name:
                    return getattr(ApiCode, name).value
        return ApiCode.INVALID_REQUEST_BODY.value

    def _first_validation_message(errors: Sequence[Any]) -> str | None:
        if not errors:
            return None
        first = errors[0]
        if isinstance(first, dict):
            msg = first.get("msg")
            if isinstance(msg, str) and msg:
                return msg
        return None

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        errors = exc.errors()
        code = _pick_validation_code(errors)
        message = _first_validation_message(errors) or ""
        return JSONResponse(
            status_code=400,
            content=_error_payload(code, message, None, request=request),
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        headers = dict(exc.headers) if exc.headers else {}
        if isinstance(exc.detail, dict) and "code" in exc.detail:
            detail = exc.detail
            code_str = detail.get("code", "")
            message = detail.get("message", "") if isinstance(detail.get("message"), str) else ""
            content = _error_payload(code_str, message, detail.get("data"), request=request)
        else:
            code_str = (HTTP_STATUS_TO_CODE.get(exc.status_code) or ApiCode.HTTP_ERROR).value
            message = ""
            if isinstance(exc.detail, str):
                message = exc.detail
            elif isinstance(exc.detail, dict) and "message" in exc.detail:
                message = exc.detail.get("message") or ""
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
        _log_error_structured(request, event, exc)
        return JSONResponse(
            status_code=500,
            content=_error_payload(
                ApiCode.DB_ERROR.value, MASKED_500_MESSAGE, None, request=request
            ),
        )

    @app.exception_handler(BaseProjectException)
    async def project_exception_handler(request: Request, exc: BaseProjectException):
        code_val = exc.code.value if isinstance(exc.code, ApiCode) else str(exc.code)
        message = getattr(exc, "message", None)
        message_str = message if isinstance(message, str) else ""
        content = _error_payload(code_val, message_str, getattr(exc, "data", None), request=request)
        return JSONResponse(status_code=exc.status_code, content=content)

    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception):
        _log_error_structured(request, "unhandled_exception", exc)
        return JSONResponse(
            status_code=500,
            content=_error_payload(
                ApiCode.INTERNAL_SERVER_ERROR.value, MASKED_500_MESSAGE, None, request=request
            ),
        )
