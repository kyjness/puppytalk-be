# ApiResponse 팩토리. Request.state.request_id 주입(X-Request-ID와 동일 값).
from typing import Any

from starlette.requests import Request

from app.common.codes import ApiCode
from app.common.schemas import ApiResponse


def get_request_id(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    return rid if isinstance(rid, str) else ""


def error_body(
    code: str,
    message: str = "",
    data: Any = None,
    *,
    request_id: str = "",
) -> dict[str, Any]:
    """에러 응답 바디의 단일 정의(ApiResponse와 동일 키). Request 객체가 없는
    순수 ASGI 미들웨어와 전역 예외 핸들러가 같은 wire 계약을 공유한다."""
    return {"code": code, "message": message, "data": data, "requestId": request_id}


def api_response(
    request: Request,
    *,
    code: ApiCode | str = ApiCode.OK,
    data: Any = None,
    message: str | None = None,
) -> ApiResponse[Any]:
    return ApiResponse(
        code=code,
        data=data,
        message=message,
        request_id=get_request_id(request),
    )


def dump_api_response(
    request: Request,
    *,
    code: ApiCode | str = ApiCode.OK,
    data: Any = None,
    message: str | None = None,
) -> dict[str, Any]:
    return api_response(request, code=code, data=data, message=message).model_dump(
        mode="json", by_alias=True
    )
