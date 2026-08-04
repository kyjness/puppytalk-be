# 환경 변수 (Settings). pydantic-settings 기반: .env 로딩·타입 강제·프로덕션 가드.
# 주의: app.common 등 상위 패키지를 import하지 않는다(Alembic env 로드 시 순환 참조 회피).
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_root = Path(__file__).resolve().parent.parent.parent
_env_file = _root / ".env"

_JWT_INSECURE_DEFAULTS = frozenset({"", "change-me-in-production"})
_MIN_JWT_SECRET_LEN = 32

# 콤마 구분 문자열을 리스트로. NoDecode로 pydantic-settings의 JSON 자동 디코딩을 끄고 직접 파싱.
_CsvList = Annotated[list[str], NoDecode]

# 하한 클램프(음수·과소값 방지). 기존 max(floor, ...) 동작 보존.
_MIN_FLOORS: dict[str, int] = {
    "DB_INIT_MAX_ATTEMPTS": 1,
    "MEDIA_CLEANUP_BATCH_SIZE": 1,
    "CELERY_BROKER_VISIBILITY_TIMEOUT": 300,
    "CELERY_RESULT_EXPIRES_SECONDS": 60,
    "CELERY_TASK_SOFT_TIME_LIMIT": 60,
    "CELERY_TASK_TIME_LIMIT": 120,
    "CELERY_TASK_IDEMPOTENCY_TTL_SECONDS": 300,
    "IDEMPOTENCY_POST_CREATE_TTL_SECONDS": 60,
    "IDEMPOTENCY_POST_CREATE_LOCK_TTL_SECONDS": 5,
    "VIEW_BUFFER_FLUSH_INTERVAL_SECONDS": 60,
    "VIEW_FLUSH_LOCK_SECONDS": 30,
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_env_file),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ----- 서버 (환경·CORS·디버그) -----
    # 참고: bind host/port는 실행 커맨드(poe run·gunicorn -b)가 직접 지정한다(설정에서 미사용).
    ENVIRONMENT: str = Field(
        default="development", validation_alias=AliasChoices("ENVIRONMENT", "ENV")
    )
    DEBUG: bool = False
    CORS_ORIGINS: _CsvList = [
        "https://puppytalk.shop",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ]
    API_PREFIX: str = "/v1"  # Nginx location /v1/ 과 일치

    # ----- DB (WRITER/READER 비우면 DB_* 로 단일 URL 사용) -----
    DB_HOST: str = "postgres"
    DB_PORT: int = 5432
    DB_USER: str = "postgres"
    DB_PASSWORD: str = ""
    DB_NAME: str = "puppytalk"
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_RECYCLE: int = 3600
    DB_PING_TIMEOUT: int = 1
    WRITER_DB_URL: str = ""
    READER_DB_URL: str = ""
    DB_INIT_MAX_ATTEMPTS: int = 3
    DB_INIT_RETRY_DELAY_SECONDS: float = 2

    # ----- JWT (배포 시 JWT_SECRET_KEY 반드시 변경) -----
    JWT_SECRET_KEY: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_SECONDS: int = 1800
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    REFRESH_TOKEN_COOKIE_NAME: str = "refresh_token"
    COOKIE_SECURE: bool = False
    # bcrypt 입력에만 덧붙임(평문+pepper). 기존 DB 해시는 pepper 없이 검증하는 폴백 유지.
    PASSWORD_PEPPER: str = ""

    # ----- 회원가입 임시 이미지 TTL 정리 -----
    SIGNUP_IMAGE_CLEANUP_INTERVAL: int = 3600
    MEDIA_CLEANUP_BATCH_SIZE: int = 200  # 이미지 정리(sweep·signup) 공통 배치 크기

    # ----- Redis (비우면 연결 시도 안 함, rate limit 등 Fail-open) -----
    REDIS_URL: str = ""
    # ----- Celery (REDIS_URL 기반 broker/result DB 인덱스 분리) -----
    CELERY_ENABLED: bool = False
    CELERY_BROKER_URL: str = ""
    CELERY_RESULT_BACKEND: str = ""
    CELERY_BROKER_DB: int = 1
    CELERY_RESULT_DB: int = 2
    CELERY_BROKER_VISIBILITY_TIMEOUT: int = 3600
    CELERY_RESULT_EXPIRES_SECONDS: int = 3600
    CELERY_TASK_SOFT_TIME_LIMIT: int = 300
    CELERY_TASK_TIME_LIMIT: int = 600
    CELERY_TASK_IDEMPOTENCY_TTL_SECONDS: int = 86400
    # SSE 알림 pubsub이 연결을 길게 점유하므로 기본 풀 크기를 넉넉히 둠.
    REDIS_MAX_CONNECTIONS: int = 128
    # POST /posts 멱등성: 성공 응답 캐시 TTL, in-flight 잠금 TTL(초)
    IDEMPOTENCY_POST_CREATE_TTL_SECONDS: int = 3600
    IDEMPOTENCY_POST_CREATE_LOCK_TTL_SECONDS: int = 120

    # ----- Proxy·Trusted Host (Nginx/ALB 뒤 배포 시) -----
    TRUST_X_FORWARDED_FOR: bool = False
    TRUSTED_PROXY_IPS: _CsvList = []
    TRUSTED_HOSTS: _CsvList = ["*"]

    # ----- Rate limit -----
    RATE_LIMIT_WINDOW: int = 60
    RATE_LIMIT_MAX_REQUESTS: int = 100
    LOGIN_RATE_LIMIT_WINDOW: int = 60
    LOGIN_RATE_LIMIT_MAX_ATTEMPTS: int = 5
    # WS는 HTTP 미들웨어를 타지 않는다 — DM 수신 루프에서 유저 단위로 적용.
    CHAT_WS_RATE_LIMIT_WINDOW: int = 60
    CHAT_WS_RATE_LIMIT_MAX_MESSAGES: int = 60
    # 유저당 인스턴스 로컬 실시간 연결(WS·SSE) 상한. 큐는 bounded지만 연결 수가 무제한이면
    # 유저 한 명이 인스턴스 로컬 상태를 무한히 늘릴 수 있다 — 탭·기기 다중 사용을 고려한 값.
    REALTIME_MAX_CONNECTIONS_PER_USER: int = 5
    # 인증 presign 유저 단위 한도 — IP 글로벌만으로는 pending/ 대량 적재를 못 막는다.
    MEDIA_PRESIGN_RATE_LIMIT_WINDOW: int = 3600
    MEDIA_PRESIGN_RATE_LIMIT_MAX: int = 100

    # ----- 회원가입 이미지 (토큰 TTL, IP당 업로드 rate limit) -----
    SIGNUP_IMAGE_TOKEN_TTL_SECONDS: int = 3600
    # 비인증 presign·confirm 각각 1회 소모 — 업로드 1건 = 2카운트이므로 "10건/시간" 의도면 20.
    SIGNUP_UPLOAD_RATE_LIMIT_WINDOW: int = 3600
    SIGNUP_UPLOAD_RATE_LIMIT_MAX: int = 20

    # ----- 파일 업로드 (허용 content-type. 크기 상한은 presigned POST 조건이 강제) -----
    ALLOWED_IMAGE_TYPES: _CsvList = ["image/jpeg", "image/png"]

    # ----- 스토리지 (S3 단일 경로. dev/CI는 MinIO=S3_ENDPOINT_URL 지정, prod는 실제 S3) -----
    S3_BUCKET_NAME: str = ""
    S3_ENDPOINT_URL: str = ""
    AWS_REGION: str = "ap-northeast-2"
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    S3_PUBLIC_BASE_URL: str = ""
    # 오프라인 푸시·다운스트림 구독용 SNS 토픽(비우면 발행 안 함)
    SNS_TOPIC_ARN: str = ""

    # ----- 로깅 -----
    LOG_LEVEL: str = "INFO"
    LOG_FILE_PATH: str = ""
    SLOW_REQUEST_MS: int = 1000

    # ----- 보안 헤더 -----
    HSTS_ENABLED: bool = False
    HSTS_MAX_AGE: int = 31536000
    REFERRER_POLICY: str = "strict-origin-when-cross-origin"
    PERMISSIONS_POLICY: str = "geolocation=(), microphone=(), camera=()"
    CONTENT_SECURITY_POLICY: str = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self';"
    )

    # ----- 신고·블라인드 -----
    REPORT_BLIND_THRESHOLD: int = 5

    # ----- 조회수 Write-behind (Redis HINCRBY → 주기적 DB flush) -----
    VIEW_BUFFER_FLUSH_INTERVAL_SECONDS: int = 300
    VIEW_FLUSH_LOCK_SECONDS: int = 120
    # 조회수 dedup 키 TTL(SET NX EX). 0 이하 = dedup 끔(같은 viewer도 매 조회 집계).
    VIEW_CACHE_TTL_SECONDS: int = 3600

    @field_validator(
        "CORS_ORIGINS", "TRUSTED_PROXY_IPS", "TRUSTED_HOSTS", "ALLOWED_IMAGE_TYPES", mode="before"
    )
    @classmethod
    def _parse_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("ENVIRONMENT", mode="after")
    @classmethod
    def _normalize_environment(cls, v: str) -> str:
        return v.strip().lower() or "development"

    @field_validator("LOG_LEVEL", mode="after")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper() or "INFO"

    @field_validator(
        "WRITER_DB_URL",
        "READER_DB_URL",
        "REDIS_URL",
        "CELERY_BROKER_URL",
        "CELERY_RESULT_BACKEND",
        "S3_ENDPOINT_URL",
        "SNS_TOPIC_ARN",
        "LOG_FILE_PATH",
        "CONTENT_SECURITY_POLICY",
        mode="after",
    )
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _clamp_minimums(self) -> "Settings":
        for name, floor in _MIN_FLOORS.items():
            if getattr(self, name) < floor:
                setattr(self, name, floor)
        return self


settings = Settings()


def validate_settings_for_environment() -> None:
    """production/prod 환경에서 안전하지 않은 설정이면 기동을 막는다(fail-fast)."""
    if settings.ENVIRONMENT not in ("production", "prod"):
        return
    errors: list[str] = []

    secret = settings.JWT_SECRET_KEY.strip()
    if secret in _JWT_INSECURE_DEFAULTS or len(secret) < _MIN_JWT_SECRET_LEN:
        errors.append("JWT_SECRET_KEY는 32자 이상의 강한 시크릿이어야 합니다.")
    if not settings.COOKIE_SECURE:
        errors.append("COOKIE_SECURE=true 여야 합니다(HTTPS 쿠키).")
    if not settings.TRUSTED_HOSTS or "*" in settings.TRUSTED_HOSTS:
        errors.append('TRUSTED_HOSTS에 와일드카드("*")를 두면 안 됩니다.')
    if not settings.DB_PASSWORD.strip():
        errors.append("DB_PASSWORD가 비어 있으면 안 됩니다.")
    if any(("localhost" in o or "127.0.0.1" in o) for o in settings.CORS_ORIGINS):
        errors.append("CORS_ORIGINS에 localhost/127.0.0.1을 두면 안 됩니다.")
    # local 디스크 백엔드 폐기(ADR 0010) → prod는 S3 자격이 반드시 있어야 업로드가 동작한다.
    if not (
        settings.S3_BUCKET_NAME.strip()
        and settings.AWS_ACCESS_KEY_ID.strip()
        and settings.AWS_SECRET_ACCESS_KEY.strip()
    ):
        errors.append(
            "S3_BUCKET_NAME·AWS_ACCESS_KEY_ID·AWS_SECRET_ACCESS_KEY가 모두 설정돼야 합니다."
        )
    # rate limit·조회수 dedup은 클라이언트 IP 단위인데, 운영 전제(ALB/Nginx 뒤)에서 XFF를
    # 신뢰하지 않으면 모든 트래픽이 소수 프록시 IP로 수렴한다 — 전역 한도가 사이트 전체를
    # 429로 조이고(자기-DoS) 익명 조회수 dedup도 프록시 단위로 붕괴한다.
    if not settings.TRUST_X_FORWARDED_FOR or not settings.TRUSTED_PROXY_IPS:
        errors.append(
            "TRUST_X_FORWARDED_FOR=true와 TRUSTED_PROXY_IPS(프록시 대역)가 설정돼야 합니다 — "
            "IP 기반 rate limit·조회수 dedup이 프록시 IP로 수렴하는 것을 방지."
        )

    if errors:
        raise ValueError(
            "프로덕션 설정 검증 실패 — 아래를 수정 후 재기동하세요:\n- " + "\n- ".join(errors)
        )
