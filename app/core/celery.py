# Celery 앱: Redis broker + result backend, 큐 라우팅, 워커 프로세스 DB 풀 초기화.

from celery import Celery
from celery.signals import worker_process_init, worker_process_shutdown

from app.core.config import settings
from app.worker.async_bridge import on_worker_process_init, on_worker_process_shutdown

_TASK_ROUTES = {
    "app.worker.tasks.notifications.*": {"queue": "high_priority"},
    "app.worker.tasks.*": {"queue": "default"},
}


def _redis_url_with_db(base: str, db_index: int) -> str:
    base = (base or "").strip().rstrip("/")
    if not base:
        return f"redis://127.0.0.1:6379/{db_index}"
    # redis://host:6379 또는 redis://host:6379/0 형태 정규화
    if "/" in base.split("://", 1)[-1]:
        prefix = base.rsplit("/", 1)[0]
        return f"{prefix}/{db_index}"
    return f"{base}/{db_index}"


def _broker_url() -> str:
    if settings.CELERY_BROKER_URL:
        return settings.CELERY_BROKER_URL
    return _redis_url_with_db(settings.REDIS_URL, settings.CELERY_BROKER_DB)


def _result_backend_url() -> str:
    if settings.CELERY_RESULT_BACKEND:
        return settings.CELERY_RESULT_BACKEND
    return _redis_url_with_db(settings.REDIS_URL, settings.CELERY_RESULT_DB)


celery_app = Celery(  # pyright: ignore[reportCallIssue]  # celery lazy import → 스텁 미비 오탐
    "puppytalk",
    broker=_broker_url(),
    backend=_result_backend_url(),
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_default_queue="default",
    task_queues={
        "default": {"exchange": "default", "routing_key": "default"},
        "high_priority": {"exchange": "high_priority", "routing_key": "high_priority"},
    },
    task_routes=_TASK_ROUTES,
    task_soft_time_limit=settings.CELERY_TASK_SOFT_TIME_LIMIT,
    task_time_limit=settings.CELERY_TASK_TIME_LIMIT,
    broker_transport_options={
        "visibility_timeout": settings.CELERY_BROKER_VISIBILITY_TIMEOUT,
        # enqueue(.delay)가 요청 경로에서 호출되므로, 블랙홀 브로커에 소켓이 매달리면
        # 그 시간만큼 댓글/좋아요 응답이 지연된다. 연결·I/O를 짧게 자르고 재시도로 넘긴다.
        "socket_timeout": 5,
        "socket_connect_timeout": 5,
    },
    # publish 실패 시 빠른 소폭 재시도 후 포기 — 호출부(_dispatch_sns_publish)가 인라인 폴백을 가진다.
    task_publish_retry_policy={
        "max_retries": 2,
        "interval_start": 0,
        "interval_step": 0.2,
        "interval_max": 0.5,
    },
    result_expires=settings.CELERY_RESULT_EXPIRES_SECONDS,
    # 결과를 읽는 곳이 없다. ignore_result가 아니면 `.delay()`가 발행 시 **결과 백엔드를
    # pubsub.subscribe** 하고(`send_task` → `backend.on_task_call`), 그 클라이언트는 위
    # broker_transport_options 밖이라 Celery 기본값(socket_timeout=120s, connect 무제한)을
    # 쓴다 — 먹통 Redis에서 요청이 인라인 폴백에 닿기 전에 그만큼 매달린다.
    task_ignore_result=True,
    # 그래도 결과 백엔드 클라이언트는 만들어지므로 방어로 공용 타임아웃을 따르게 한다.
    redis_socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
    redis_socket_connect_timeout=settings.REDIS_SOCKET_CONNECT_TIMEOUT,
)

celery_app.autodiscover_tasks(["app.worker.tasks"])


@worker_process_init.connect
def _celery_worker_process_init(**_kwargs: object) -> None:
    on_worker_process_init()


@worker_process_shutdown.connect
def _celery_worker_process_shutdown(**_kwargs: object) -> None:
    on_worker_process_shutdown()
