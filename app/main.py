# PuppyTalk API 진입점. lifespan, 미들웨어·라우터·/health. DI는 app.api.dependencies.
import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.v1 import v1_router
from app.common import ApiCode, ApiResponse, RootData, api_response, setup_logging
from app.core.cleanup import PeriodicJob, run_jobs_once, run_periodic
from app.core.config import settings, validate_settings_for_environment
from app.core.exception_handlers import register_exception_handlers
from app.core.middleware import (
    RequestIdMiddleware,
    observability_middleware,
    render_metrics,
)
from app.core.middleware.proxy_headers import ProxyHeadersMiddleware
from app.core.middleware.rate_limit import RateLimitMiddleware
from app.core.openapi_camel import openapi_schema_to_camel
from app.db import check_database
from app.infra.redis import get_app_redis


def _cleanup_jobs(redis_client: Any) -> tuple[PeriodicJob, ...]:
    """주기 정리 잡 정의(보존 정책 포함). 러너(core.cleanup)는 무엇을 도는지 모른다."""
    from app.db import get_connection
    from app.domain.media.service import MediaService
    from app.domain.notifications.service import NotificationService
    from app.domain.users.service import UserService

    job_log = logging.getLogger("app.cleanup")

    async def signup_image_cleanup(task_id: str) -> int:
        async with get_connection() as db:
            deleted_count, failed_file_keys = await MediaService.cleanup_expired_signup_images(
                db, task_id=task_id, redis=redis_client
            )
        if failed_file_keys:
            job_log.warning(
                "signup_image_cleanup_partial task_id=%s deleted_count=%s storage_delete_failed=%s keys=%s",
                task_id,
                deleted_count,
                len(failed_file_keys),
                failed_file_keys,
            )
            job_log.warning(
                "[S3_DELETE_RETRY_NEEDED] task_id=%s keys=%s", task_id, failed_file_keys
            )
        return deleted_count

    async def orphan_post_image_cleanup(_task_id: str) -> int:
        # 게시글 작성 중 이탈 등으로 남은 고아 이미지(24h+) 정리
        async with get_connection() as db:
            return await MediaService.sweep_unused_images(db, redis=redis_client)

    async def withdrawn_user_purge(_task_id: str) -> int:
        # 탈퇴 유저 파기(30일 경과 하드 삭제, 청크 단위)
        async with get_connection() as db:
            return await UserService.purge_withdrawn_users(older_than_days=30, db=db)

    async def notification_purge(_task_id: str) -> int:
        # 알림 자동 삭제(30일 경과)
        async with get_connection() as db:
            return await NotificationService.purge_old_notifications(
                older_than_days=30, chunk_size=2_000, db=db
            )

    return (
        PeriodicJob("signup_image_cleanup", signup_image_cleanup),
        PeriodicJob("orphan_post_image_cleanup", orphan_post_image_cleanup),
        # 퍼지 2종은 테이블이 빌 때까지 청크 삭제를 반복해 백로그가 쌓였을 때 오래 돈다 —
        # 락이 도중에 만료돼 다른 인스턴스가 끼어들지 않도록 TTL을 넉넉히 준다.
        PeriodicJob("withdrawn_user_purge", withdrawn_user_purge, lock_ttl_seconds=1800),
        PeriodicJob("notification_purge", notification_purge, lock_ttl_seconds=1800),
    )


async def _view_buffer_flush_loop(stop_event: asyncio.Event, redis_client: Any) -> None:
    """조회수 Redis 버퍼를 주기적으로 DB에 반영."""
    flush_log = logging.getLogger("app.view_buffer_flush")
    interval = settings.VIEW_BUFFER_FLUSH_INTERVAL_SECONDS
    from app.domain.posts.services import PostService

    while True:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            break
        except TimeoutError:
            pass
        if stop_event.is_set():
            break
        try:
            await PostService.flush_view_counts_to_db(redis_client)
        except Exception:
            flush_log.exception("조회수 버퍼 flush 실패")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.db import close_database, init_database
    from app.infra.redis import close_redis, init_redis

    validate_settings_for_environment()
    setup_logging()
    log = logging.getLogger(__name__)
    if not await init_database():
        log.critical(
            "PostgreSQL 연결을 %s회 시도했으나 실패. 프로세스를 종료합니다.",
            settings.DB_INIT_MAX_ATTEMPTS,
        )
        sys.exit(1)
    log.info("PostgreSQL 연결 성공.")

    await init_redis(app)

    redis_client = get_app_redis(app)
    cleanup_jobs = _cleanup_jobs(redis_client)
    await run_jobs_once(cleanup_jobs, redis=redis_client)
    stop_event = asyncio.Event()
    cleanup_task = None
    view_flush_task: asyncio.Task[None] | None = None
    fanout_listener_task: asyncio.Task[None] | None = None
    if settings.SIGNUP_IMAGE_CLEANUP_INTERVAL > 0:
        cleanup_task = asyncio.create_task(
            run_periodic(
                cleanup_jobs,
                stop_event,
                interval_seconds=max(60, settings.SIGNUP_IMAGE_CLEANUP_INTERVAL),
                redis=redis_client,
            )
        )
    if redis_client is not None and settings.VIEW_BUFFER_FLUSH_INTERVAL_SECONDS > 0:
        view_flush_task = asyncio.create_task(_view_buffer_flush_loop(stop_event, redis_client))
    if settings.REDIS_URL:
        # 인스턴스당 전용 Pub/Sub 연결 1개로 chat DM(WS)·알림(SSE) 채널을 함께 구독.
        # app.state.redis(부팅 핑 성공)에 게이트하지 않는다 — 리스너는 자기 연결을
        # 백오프로 재시도하므로, 배포 중 Redis 순단이 크로스 인스턴스 실시간 전달을
        # 프로세스 수명 내내 비활성화해서는 안 된다.
        from app.domain.chat.manager import CHAT_DM_FANOUT_CHANNEL, chat_connection_manager
        from app.domain.notifications.stream import (
            NOTIF_SSE_FANOUT_CHANNEL,
            notification_sse_manager,
        )
        from app.infra.pubsub import run_user_fanout_listener

        fanout_listener_task = asyncio.create_task(
            run_user_fanout_listener(
                redis_url=settings.REDIS_URL,
                handlers={
                    CHAT_DM_FANOUT_CHANNEL: chat_connection_manager.send_personal_message,
                    NOTIF_SSE_FANOUT_CHANNEL: notification_sse_manager.deliver,
                },
                stop_event=stop_event,
            )
        )

    yield

    stop_event.set()
    if cleanup_task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(cleanup_task), timeout=15.0)
        except TimeoutError:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass  # Intended: swallow cancel on lifespan shutdown
    if view_flush_task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(view_flush_task), timeout=30.0)
        except TimeoutError:
            view_flush_task.cancel()
            try:
                await view_flush_task
            except asyncio.CancelledError:
                pass
    if fanout_listener_task is not None:
        fanout_listener_task.cancel()
        try:
            await fanout_listener_task
        except asyncio.CancelledError:
            pass
    # 인라인 SNS 폴백 태스크를 redis close 전에 드레인 — publish와 멱등 마킹 사이에서
    # 끊기면 워커 재시도 시 이중 배송 창이 다시 열린다.
    from app.domain.notifications.service import drain_sns_inline_tasks

    await drain_sns_inline_tasks()
    await close_redis(app)
    await close_database()


# 1. 설정값 가져오기
_prefix = settings.API_PREFIX.rstrip("/")

app = FastAPI(
    title="PuppyTalk API",
    description="커뮤니티 백엔드 API",
    version="1.0.0",
    lifespan=lifespan,
    # 2. Swagger 및 OpenAPI 경로를 prefix에 맞게 동적 설정
    docs_url=f"{_prefix}/docs",
    redoc_url=f"{_prefix}/redoc",
    openapi_url=f"{_prefix}/openapi.json",
)

# LIFO: 먼저 등록할수록 안쪽(라우트에 가깝다), 마지막 등록이 가장 바깥 껍질.
#
# RateLimit은 최안쪽 — 429가 CORS·metrics·access_log를 "거쳐 나가야" 브라우저가
# CORS 에러 대신 429+Retry-After를 읽고, RED 메트릭·접근로그에도 잡힌다. 429는 라우트
# 매칭 전에 끊기므로 메트릭 path 라벨은 __unmatched__로 집계된다(카디널리티 보호 유지).
# 클라이언트 IP는 더 바깥의 ProxyHeaders가 scope에 반영한 뒤라 키 산정에 문제 없다.
app.add_middleware(RateLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
if settings.TRUSTED_HOSTS != ["*"]:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.TRUSTED_HOSTS)
# RequestIdMiddleware가 가장 바깥 → 요청 진입 즉시 request_id 발급(에러 응답 포함 전 구간 전파).
# GZip은 관측 미들웨어보다 바깥에 두어 압축 시간이 duration 측정을 오염시키지 않게 한다.
# 보안 헤더·접근 로그·RED 메트릭은 한 겹(observability)으로 — 층당 task·스트림 할당 절감.
app.middleware("http")(observability_middleware)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(ProxyHeadersMiddleware)
app.add_middleware(RequestIdMiddleware)

register_exception_handlers(app)


@app.get("/")
def alb_health_check():
    return {"status": "ok", "message": "PuppyTalk API is running!"}


@app.get("/livez")
def livez():
    """Liveness probe — 프로세스 생존만 판정(의존성 체크 없음). 실패 시 컨테이너 재시작 신호."""
    return {"status": "alive"}


@app.get("/readyz")
async def readyz(request: Request):
    """Readiness probe — 트래픽 수용 가능 여부. DB=hard(없으면 503), Redis=soft(fail-open이라 report만).

    ECS/ALB 타깃 헬스·k8s readiness가 이 경로로 라우팅 제외를 판단한다.
    """
    db_ok = await check_database()
    redis = get_app_redis(request.app)
    redis_ok = False
    if redis is not None:
        try:
            redis_ok = bool(await redis.ping())
        except Exception:
            redis_ok = False
    ready = db_ok  # DB만 hard 의존성. Redis는 미연결이어도 서빙 가능(rate limit fail-open).
    payload = {
        "status": "ready" if ready else "not_ready",
        "db": "ok" if db_ok else "down",
        "redis": "ok" if redis_ok else "down",
    }
    if ready:
        return payload
    return JSONResponse(status_code=503, content=payload)


@app.get("/metrics")
def metrics():
    """Prometheus 스크레이프 엔드포인트(비-prefix). 스크레이퍼가 주기적으로 pull."""
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)


# 3. 루트 및 헬스체크용 공통 라우터 생성
base_router = APIRouter(prefix=_prefix)


@base_router.get("/", response_model=ApiResponse[RootData])
def root(request: Request):
    return api_response(
        request,
        code=ApiCode.OK,
        data=RootData(
            message="PuppyTalk API is running!",
            version="1.0.0",
            docs=f"{_prefix}/docs",
        ),
    )


@base_router.get("/health", response_model=ApiResponse[dict[str, str]])
async def health(request: Request):
    ok = await check_database()
    payload = api_response(
        request,
        code=ApiCode.OK if ok else ApiCode.DB_ERROR,
        data={"status": "ok" if ok else "degraded"},
    )
    if ok:
        return payload
    return JSONResponse(
        status_code=503,
        content=payload.model_dump(mode="json", by_alias=True),
    )


# 4. 라우터 등록 (순서 중요)
app.include_router(base_router)  # /v1, /v1/health 등록
app.include_router(v1_router)  # /v1/auth, /v1/users 등 기존 도메인 등록


# 5. OpenAPI 스키마를 실제 응답(camelCase)과 일치시키기 위해 스키마 property 키를 camelCase로 변환
def _custom_openapi():
    if app.openapi_schema is not None:
        return app.openapi_schema
    app.openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=getattr(app, "openapi_version", "3.1.0"),
        description=app.description,
        routes=app.routes,
        tags=getattr(app, "openapi_tags", None),
        servers=getattr(app, "servers", None),
    )
    app.openapi_schema = openapi_schema_to_camel(app.openapi_schema)
    return app.openapi_schema


app.openapi = _custom_openapi
