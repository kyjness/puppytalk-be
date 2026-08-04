# 도메인 기반 커스텀 예외. Service에서 raise → 전역 handler가 { code, data, message } 규격으로 변환.
# core 계층이 특정 Model에 의존하지 않도록 예외 객체만으로 응답 구성.
# 서브클래스는 status_code·code·default_message를 클래스 속성으로 선언만 한다 — __init__ 불필요.
from typing import Any

from app.common.codes import ApiCode
from app.common.responses import retry_after_fields


class BaseProjectException(Exception):
    """프로젝트 공통 예외. status_code, code, message, data(+headers)로 전역 handler가 JSON 응답 생성."""

    status_code: int = 500
    code: ApiCode = ApiCode.INTERNAL_SERVER_ERROR
    default_message: str | None = None
    headers: dict[str, str] | None = None

    def __init__(self, message: str | None = None, *, data: Any | None = None):
        # message가 없고 default_message도 없으면 None 유지 — 핸들러가 ""로 응답한다.
        # str(code)로 백필하면 응답 바디가 바뀌므로 금지. data는 무가공 패스스루(None 포함).
        self.message = message or self.default_message
        self.data = data
        super().__init__(self.message or str(self.code))


# --- Posts ---
class PostNotFoundException(BaseProjectException):
    status_code = 404
    code = ApiCode.POST_NOT_FOUND


class ConcurrentUpdateException(BaseProjectException):
    """낙관적 락 충돌(예: SQLAlchemy StaleDataError) 시 반환하는 409 예외."""

    status_code = 409
    code = ApiCode.CONFLICT
    default_message = "데이터가 다른 요청에 의해 변경되어 완료할 수 없습니다."


# --- Users / Auth ---
class UserNotFoundException(BaseProjectException):
    status_code = 404
    code = ApiCode.USER_NOT_FOUND


class UserWithdrawnException(BaseProjectException):
    status_code = 400
    code = ApiCode.USER_WITHDRAWN
    default_message = "탈퇴한 유저입니다."


class EmailAlreadyExistsException(BaseProjectException):
    status_code = 409
    code = ApiCode.EMAIL_ALREADY_EXISTS


class NicknameAlreadyExistsException(BaseProjectException):
    status_code = 409
    code = ApiCode.NICKNAME_ALREADY_EXISTS


class MissingRequiredFieldException(BaseProjectException):
    status_code = 400
    code = ApiCode.MISSING_REQUIRED_FIELD


class SignupImageTokenInvalidException(BaseProjectException):
    status_code = 400
    code = ApiCode.SIGNUP_IMAGE_TOKEN_INVALID


class InvalidCredentialsException(BaseProjectException):
    """이메일/비밀번호 불일치 등 로그인 실패(401)."""

    status_code = 401
    code = ApiCode.INVALID_CREDENTIALS
    default_message = "이메일 또는 비밀번호가 일치하지 않습니다"


class UnauthorizedException(BaseProjectException):
    status_code = 401
    code = ApiCode.UNAUTHORIZED


class ForbiddenException(BaseProjectException):
    status_code = 403
    code = ApiCode.FORBIDDEN


# --- Chat ---
class SelfDmException(BaseProjectException):
    """자기 자신과의 DM 시도(400). REST·WS 공용 — WS 표면은 code 문자열이 에러 프레임에 노출된다."""

    status_code = 400
    code = ApiCode.DM_SAME_USER
    default_message = "자기 자신과는 채팅할 수 없습니다."


# --- Comments ---
class CommentNotFoundException(BaseProjectException):
    status_code = 404
    code = ApiCode.COMMENT_NOT_FOUND


class InvalidPostIdFormatException(BaseProjectException):
    status_code = 400
    code = ApiCode.INVALID_POSTID_FORMAT


# --- Media / Image ---
class ImageNotFoundException(BaseProjectException):
    status_code = 404
    code = ApiCode.IMAGE_NOT_FOUND


class InvalidImageFileException(BaseProjectException):
    """이미지 파일 형식/포맷 오류(400)."""

    status_code = 400
    code = ApiCode.INVALID_IMAGE_FILE


class InvalidFileTypeException(BaseProjectException):
    status_code = 400
    code = ApiCode.INVALID_FILE_TYPE


# --- 공통 ---
class InternalServerErrorException(BaseProjectException):
    """500 — base 기본값(500/INTERNAL_SERVER_ERROR) 그대로. 생성자 message는
    클라이언트에 노출되지 않는다(5xx 마스킹) — 서버 로그 전용."""


class InvalidRequestException(BaseProjectException):
    status_code = 400
    code = ApiCode.INVALID_REQUEST


class TooManyRequestsException(BaseProjectException):
    """라우트/서비스 계층 rate limit 초과(429). 미들웨어 429와 동일한 data 규격
    (retry_after_fields)·Retry-After 헤더로 클라이언트가 두 경로를 구분 없이 처리한다."""

    status_code = 429
    code = ApiCode.RATE_LIMIT_EXCEEDED

    def __init__(self, *, retry_after_seconds: int = 0, message: str | None = None):
        data, header_value = retry_after_fields(retry_after_seconds)
        super().__init__(message, data=data)
        self.headers = {"Retry-After": header_value}


class NotFoundException(BaseProjectException):
    """도메인 전용 코드가 없는 일반 404."""

    status_code = 404
    code = ApiCode.NOT_FOUND


def ws_close_code(exc: BaseProjectException) -> int:
    """WebSocket 표면의 예외 → close code. 선언된 status_code에서 파생한다 —
    4xx는 1008(정책 위반), 5xx는 1011(내부 오류). 예외별 매핑을 따로 두지 않는다."""
    return 1011 if exc.status_code >= 500 else 1008
