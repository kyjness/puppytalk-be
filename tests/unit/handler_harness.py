"""전역 예외 핸들러 호출 공용 하네스.

핸들러 등록·조회 방식(FastAPI 앱 구축 + app.exception_handlers[예외 키])이 바뀌면
여기 한 곳만 고친다. 앱은 모듈 스코프에서 1회만 구축해 재사용한다.
"""

import json
from typing import Any, cast

from app.core.exception_handlers import register_exception_handlers
from fastapi import FastAPI
from starlette.requests import Request

_app = FastAPI()
register_exception_handlers(_app)


def make_request(method: str = "POST", path: str = "/t") -> Request:
    return Request({"type": "http", "method": method, "path": path, "headers": []})


async def invoke_handler(key: Any, exc: BaseException) -> Any:
    """key(등록 시 사용한 예외 클래스)로 핸들러를 찾아 직접 호출한다."""
    handler = cast(Any, _app.exception_handlers[key])
    return await handler(make_request(), exc)


def body_of(resp: Any) -> dict[str, Any]:
    return json.loads(bytes(resp.body))
