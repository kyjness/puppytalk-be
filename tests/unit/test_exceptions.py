"""선언형 예외 계층 + 전역 핸들러 계약 단위 테스트.

고정하는 계약: (status_code, code) 선언 표, message 미지정 시 None 유지(핸들러가 ""로 응답 —
str(code) 백필 금지), data 무가공 패스스루, 429의 data·Retry-After 헤더 규격,
5xx의 로깅+메시지 마스킹(다른 500 경로와 동일 정책).
"""

import json
import logging
from typing import cast

import pytest
from app.common.codes import ApiCode
from app.common.exceptions import (
    BaseProjectException,
    CommentNotFoundException,
    ConcurrentUpdateException,
    EmailAlreadyExistsException,
    ForbiddenException,
    ImageNotFoundException,
    InternalServerErrorException,
    InvalidCredentialsException,
    InvalidFileTypeException,
    InvalidImageFileException,
    InvalidPostIdFormatException,
    InvalidRequestException,
    MissingRequiredFieldException,
    NicknameAlreadyExistsException,
    NotFoundException,
    PostNotFoundException,
    SignupImageTokenInvalidException,
    TooManyRequestsException,
    UnauthorizedException,
    UserNotFoundException,
    UserWithdrawnException,
)
from app.core.exception_handlers import MASKED_500_MESSAGE
from fastapi.exceptions import RequestValidationError

from tests.unit.handler_harness import body_of, invoke_handler

pytestmark = pytest.mark.asyncio

# (클래스, status_code, code) 선언 표 — 와이어 계약의 단일 대조표.
DECLARATIONS = [
    (PostNotFoundException, 404, ApiCode.POST_NOT_FOUND),
    (ConcurrentUpdateException, 409, ApiCode.CONFLICT),
    (UserNotFoundException, 404, ApiCode.USER_NOT_FOUND),
    (UserWithdrawnException, 400, ApiCode.USER_WITHDRAWN),
    (EmailAlreadyExistsException, 409, ApiCode.EMAIL_ALREADY_EXISTS),
    (NicknameAlreadyExistsException, 409, ApiCode.NICKNAME_ALREADY_EXISTS),
    (MissingRequiredFieldException, 400, ApiCode.MISSING_REQUIRED_FIELD),
    (SignupImageTokenInvalidException, 400, ApiCode.SIGNUP_IMAGE_TOKEN_INVALID),
    (InvalidCredentialsException, 401, ApiCode.INVALID_CREDENTIALS),
    (UnauthorizedException, 401, ApiCode.UNAUTHORIZED),
    (ForbiddenException, 403, ApiCode.FORBIDDEN),
    (CommentNotFoundException, 404, ApiCode.COMMENT_NOT_FOUND),
    (InvalidPostIdFormatException, 400, ApiCode.INVALID_POSTID_FORMAT),
    (ImageNotFoundException, 404, ApiCode.IMAGE_NOT_FOUND),
    (InvalidImageFileException, 400, ApiCode.INVALID_IMAGE_FILE),
    (InvalidFileTypeException, 400, ApiCode.INVALID_FILE_TYPE),
    (InternalServerErrorException, 500, ApiCode.INTERNAL_SERVER_ERROR),
    (InvalidRequestException, 400, ApiCode.INVALID_REQUEST),
    (TooManyRequestsException, 429, ApiCode.RATE_LIMIT_EXCEEDED),
    (NotFoundException, 404, ApiCode.NOT_FOUND),
]


@pytest.mark.parametrize(("cls", "status", "code"), DECLARATIONS)
async def test_declaration_table(cls, status, code):
    exc = cls()
    assert exc.status_code == status
    assert exc.code is code


async def test_no_arg_message_stays_none_unless_default():
    # default_message 없는 클래스: message는 None 유지(핸들러가 ""로 응답), str(exc)는 code 값.
    exc = PostNotFoundException()
    assert exc.message is None
    assert str(exc) == ApiCode.POST_NOT_FOUND.value
    # default_message 있는 클래스: message·str(exc) 모두 기본 메시지.
    conflict = ConcurrentUpdateException()
    assert conflict.message == "데이터가 다른 요청에 의해 변경되어 완료할 수 없습니다."
    assert str(conflict) == conflict.message


async def test_positional_arg_is_message():
    exc = ConcurrentUpdateException("커스텀 메시지")
    assert exc.message == "커스텀 메시지"
    assert str(exc) == "커스텀 메시지"
    # 일반 404도 균일 시그니처 — 첫 위치 인자는 항상 message(과거 NotFoundException은 code였음).
    nf = NotFoundException("강아지를 찾을 수 없습니다.")
    assert nf.message == "강아지를 찾을 수 없습니다."
    assert nf.code is ApiCode.NOT_FOUND


async def test_data_passthrough_default_none():
    assert PostNotFoundException().data is None
    payload = {"field": "value"}
    assert InvalidRequestException(data=payload).data is payload


async def test_too_many_requests_data_and_header():
    exc = TooManyRequestsException(retry_after_seconds=42)
    assert exc.data == {"retry_after_seconds": 42}
    assert exc.headers == {"Retry-After": "42"}
    # 0이어도 data는 항상 설정(미들웨어 429와 규격 동일).
    assert TooManyRequestsException().data == {"retry_after_seconds": 0}


async def test_instances_do_not_share_data_dict():
    # 가변 기본값 공유 회귀 가드 — data dict가 인스턴스별로 독립이어야 한다.
    a = TooManyRequestsException(retry_after_seconds=1)
    b = TooManyRequestsException(retry_after_seconds=2)
    assert a.data is not b.data
    cast(dict, a.data)["retry_after_seconds"] = 99
    assert b.data == {"retry_after_seconds": 2}
    assert BaseProjectException.headers is None  # 클래스 기본값 오염 없음


# --- 전역 핸들러 계약 (호출 하네스는 handler_harness가 단일 소유) ---


async def test_handler_4xx_body_and_empty_message():
    resp = await invoke_handler(BaseProjectException, PostNotFoundException())
    body = body_of(resp)
    assert resp.status_code == 404
    assert body["code"] == "POST_NOT_FOUND"
    assert body["message"] == ""  # message=None → ""(str(code) 백필 금지)
    assert body["data"] is None


async def test_handler_429_sets_retry_after_header():
    resp = await invoke_handler(
        BaseProjectException, TooManyRequestsException(retry_after_seconds=7)
    )
    body = body_of(resp)
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "7"
    assert body["data"] == {"retry_after_seconds": 7}


async def test_framework_http_exception_gets_envelope():
    # 라우팅 404/405는 Starlette가 starlette.exceptions.HTTPException을 직접 raise한다.
    # 핸들러 조회는 MRO 기반이라 Starlette 기반 클래스로 등록해야 잡힌다 — fastapi.HTTPException
    # 키로 등록하면 프레임워크 404가 {"detail": ...} 기본 응답으로 새는 회귀를 고정.
    from starlette.exceptions import HTTPException as StarletteHTTPException

    exc = StarletteHTTPException(status_code=404, detail="Not Found")
    resp = await invoke_handler(StarletteHTTPException, exc)
    body = body_of(resp)
    assert resp.status_code == 404
    assert body["code"] == "NOT_FOUND"
    assert body["message"] == "Not Found"
    assert "detail" not in body


async def test_handler_5xx_masks_message_data_and_logs(caplog):
    # 인프라 상세가 message·data 어느 쪽으로도 새지 않고, 서버 로그에는 남는다
    # (다른 500 경로와 동일 정책 — _masked_500_response 단일 조립).
    with caplog.at_level(logging.ERROR):
        resp = await invoke_handler(
            BaseProjectException,
            InternalServerErrorException("Redis unavailable for tokens", data={"redis": "down"}),
        )
    body = body_of(resp)
    assert resp.status_code == 500
    assert body["message"] == MASKED_500_MESSAGE
    assert body["data"] is None  # 5xx data 마스킹 — message만 가리는 반쪽 정책 회귀 가드
    assert "Redis unavailable" not in json.dumps(body)
    assert any("Redis unavailable" in r.message for r in caplog.records)


# --- 검증(422→400) 핸들러: code·message 짝 일치 + 정확 토큰 해석 ---


async def _validation_body(errors: list[dict]) -> dict:
    resp = await invoke_handler(RequestValidationError, RequestValidationError(errors))
    assert resp.status_code == 400
    return body_of(resp)


async def test_validation_code_and_message_from_same_error():
    # 코드는 에러 N에서, 메시지는 에러 0에서 뽑아 짝이 어긋나던 결함의 회귀 가드.
    body = await _validation_body(
        [
            {"msg": "value is not a valid email address", "loc": ("body", "email")},
            {"msg": "Value error, INVALID_PASSWORD_FORMAT", "loc": ("body", "password")},
        ]
    )
    assert body["code"] == "INVALID_PASSWORD_FORMAT"
    assert body["message"] == "Value error, INVALID_PASSWORD_FORMAT"


async def test_validation_exact_token_no_substring_collision():
    # INVALID_REQUEST ⊂ INVALID_REQUEST_BODY — 정확 일치라 순서 규칙 없이도 안 섞인다.
    assert (await _validation_body([{"msg": "Value error, INVALID_REQUEST_BODY"}]))["code"] == (
        "INVALID_REQUEST_BODY"
    )
    assert (await _validation_body([{"msg": "Value error, INVALID_REQUEST"}]))["code"] == (
        "INVALID_REQUEST"
    )


async def test_validation_any_apicode_producer_resolves():
    # 큐레이션 테이블 시절 누락됐던 생산자(post_schema의 해시태그 상한)도 자동 해석된다.
    body = await _validation_body([{"msg": "Value error, POST_HASHTAG_LIMIT_EXCEEDED"}])
    assert body["code"] == "POST_HASHTAG_LIMIT_EXCEEDED"


async def test_validation_fallback_uses_first_message():
    body = await _validation_body(
        [{"msg": "value is not a valid email address"}, {"msg": "field required"}]
    )
    assert body["code"] == "INVALID_REQUEST_BODY"
    assert body["message"] == "value is not a valid email address"
