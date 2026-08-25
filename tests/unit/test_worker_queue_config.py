"""워커 큐(arq) 연결 설정이 요청 경로를 Redis에 매달리게 하지 않는지 (ADR 0005·0020).

`enqueue_job`은 알림 발행 요청 경로에서 불린다. arq는 redis-py 클라이언트를 자기
`RedisSettings`로 직접 만들므로 `app/infra/redis.py::redis_connection_kwargs()`를 거치지
않는다 — 네 번째 생성부다. 그대로 두면 ADR 0005가 막으려던 "생성부마다 타임아웃이 갈린다"가
재발한다.

이 파일이 대체한 `test_celery_config.py`는 *"결과 백엔드 pubsub 구독이 공용 타임아웃 밖이라
먹통 Redis에서 120초 매달린다"* 를 막던 가드였다. arq엔 그 구독 자체가 없어 위험이 구조적으로
사라졌고, 남는 계약은 **연결 설정이 공용 값을 따르는가** 하나다.
"""

import pytest
from app.core.config import settings
from app.infra.redis import arq_redis_settings

_DSN = "redis://localhost:6379/0"


def test_returns_none_without_redis_url(monkeypatch):
    """Redis가 없으면 큐도 없다 — 호출부는 인라인 폴백으로 간다(fail-open)."""
    monkeypatch.setattr(settings, "REDIS_URL", "")
    assert arq_redis_settings() is None


def test_conn_timeout_follows_shared_setting(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_URL", _DSN)
    monkeypatch.setattr(settings, "REDIS_SOCKET_CONNECT_TIMEOUT", 2.5)

    rs = arq_redis_settings()
    assert rs is not None
    # arq의 conn_timeout은 int라 올림한다 — 내림하면 설정값보다 짧아져 정상 연결이 끊긴다.
    assert rs.conn_timeout == 3, "arq 자체 기본값을 쓰면 앱 공용 타임아웃과 갈린다 (ADR 0005)"


def test_connection_retries_are_capped(monkeypatch):
    """arq 기본은 5회 × 1초 지연 — 요청 경로에서 죽은 Redis에 5초 넘게 매달린다."""
    monkeypatch.setattr(settings, "REDIS_URL", _DSN)

    rs = arq_redis_settings()
    assert rs is not None
    assert rs.conn_retries == 1, "인라인 폴백에 빨리 닿는 쪽이 맞다 (ADR 0005)"


def test_queue_uses_separate_redis_db(monkeypatch):
    """큐 키가 앱 키스페이스(rate limit·캐시·조회수 버퍼)를 오염시키지 않게 인덱스를 나눈다."""
    monkeypatch.setattr(settings, "REDIS_URL", _DSN)
    monkeypatch.setattr(settings, "ARQ_REDIS_DB", 1)

    rs = arq_redis_settings()
    assert rs is not None
    assert rs.database == 1


def test_worker_does_not_retain_job_results():
    """결과를 읽는 곳이 없다 — 보관하면 Redis에 쓰레기만 쌓인다."""
    from app.worker.settings import WorkerSettings

    assert WorkerSettings.keep_result == 0


def test_worker_registers_the_delivery_job():
    """enqueue가 부르는 이름(`deliver_notification_sns`)이 실제 등록돼 있어야 잡이 실행된다."""
    from app.worker.settings import WorkerSettings

    assert [f.__name__ for f in WorkerSettings.functions] == ["deliver_notification_sns"]


@pytest.mark.parametrize("job_try,lo,hi", [(1, 60, 90), (2, 120, 180), (3, 240, 360)])
def test_retry_backoff_grows_and_is_jittered(job_try, lo, hi):
    """Celery의 retry_backoff·retry_jitter와 같은 곡선 — 지터가 없으면 재시도가 한 초에 몰린다."""
    from app.worker.jobs.notification_delivery import _retry_delay

    delay = _retry_delay(job_try)
    assert lo <= delay <= hi


async def _run_job(monkeypatch, *, raises: Exception | None):
    """잡 본문을 몽키패치해 재시도 판정만 검사한다."""
    import app.worker.jobs.notification_delivery as tasks_mod

    async def _fake(**kwargs):
        if raises is not None:
            raise raises
        return {"status": "delivered"}

    monkeypatch.setattr(tasks_mod, "deliver_notification_sns_async", _fake)
    return await tasks_mod.deliver_notification_sns(
        {"job_try": 1}, notification_id="n", user_id="u", idempotency_key="k"
    )


@pytest.mark.asyncio
async def test_generic_failure_raises_retry(monkeypatch):
    """**arq는 평범한 예외를 재시도하지 않는다** — `Retry`로 바꾸지 않으면 배송이 조용히 유실된다.

    Celery는 `self.retry(exc=...)`가 예외를 삼켜 재시도했지만 arq는 시맨틱이 반대다.
    이 교체에서 가장 놓치기 쉬운 지점이라 계약으로 못 박는다(ADR 0020 결정 2).
    """
    from arq.worker import Retry

    with pytest.raises(Retry):
        await _run_job(monkeypatch, raises=ConnectionError("sns down"))


@pytest.mark.asyncio
async def test_skip_does_not_retry(monkeypatch):
    """재시도해도 결과가 같은 경우(미존재·멱등 스킵·SNS 비활성)는 Retry를 올리지 않는다."""
    from app.worker.jobs.notification_delivery import NotificationDeliverySkip

    result = await _run_job(
        monkeypatch, raises=NotificationDeliverySkip("sns topic not configured")
    )
    assert result["status"] == "skipped"


@pytest.mark.asyncio
async def test_success_returns_job_result(monkeypatch):
    result = await _run_job(monkeypatch, raises=None)
    assert result["status"] == "delivered"


@pytest.mark.asyncio
async def test_worker_refuses_to_start_without_redis_config(monkeypatch):
    """arq는 redis_settings=None이면 조용히 localhost:6379/db 0(앱 키스페이스)으로 붙는다.

    발행측은 같은 상황에서 인라인 폴백으로 떨어지지만(fail-open), 워커가 엉뚱한 Redis를 잡는
    것은 열화가 아니라 오염이라 여기만 fail-fast다. `on_startup` raise는 poll 루프 전이라
    잡을 하나도 집지 않는다.
    """
    from app.worker.settings import WorkerSettings, on_startup

    monkeypatch.setattr(WorkerSettings, "redis_settings", None)

    with pytest.raises(RuntimeError, match="REDIS_URL"):
        await on_startup({})


def test_retry_base_seconds_has_a_floor():
    """0이면 재시도 4회가 지연 없이 즉시 소진돼 지터가 막으려던 thundering herd가 열린다.

    형제 설정(`ARQ_JOB_TIMEOUT`·`ARQ_MAX_TRIES`)은 `_MIN_FLOORS`에 있는데 이것만 빠져 있었다 —
    이미 있는 클램프 메커니즘에 옵트인하지 않은 것이 결함이었다.
    """
    from app.core.config import _MIN_FLOORS

    assert _MIN_FLOORS.get("ARQ_RETRY_BASE_SECONDS", 0) >= 1


@pytest.mark.asyncio
async def test_queue_pool_carries_command_timeout(monkeypatch):
    """arq는 명령 타임아웃을 안 건다 — 연결만 유한하고 명령은 무한이다.

    `RedisSettings`에 그 필드가 없어 `create_pool`은 `conn_timeout`을 `socket_connect_timeout`
    으로만 넘긴다. 그대로 두면 Redis가 *먹통*(TCP는 살아 있고 응답만 안 옴)일 때
    `enqueue_job`이 요청 경로에서 영원히 매달리고, 예외가 안 나니 인라인 폴백도 발동하지
    않는다 — ADR 0005가 막으려는 바로 그 구멍이다. 지운 Celery 설정에는 이 가드가 있었다.
    """
    import app.infra.queue as queue_mod

    class _FakePool:
        def __init__(self):
            self.connection_kwargs: dict = {"socket_connect_timeout": 2}
            self.reset_called = False

        def reset(self):
            self.reset_called = True

    class _FakeArqRedis:
        def __init__(self):
            self.connection_pool = _FakePool()

    fake = _FakeArqRedis()

    async def _fake_create_pool(_rs):
        return fake

    monkeypatch.setattr(settings, "WORKER_ENABLED", True)
    monkeypatch.setattr(settings, "REDIS_URL", _DSN)
    monkeypatch.setattr("arq.connections.create_pool", _fake_create_pool)
    monkeypatch.setattr(queue_mod, "_queue", None)

    await queue_mod.init_queue()

    ck = fake.connection_pool.connection_kwargs
    assert ck["socket_timeout"] == settings.REDIS_SOCKET_TIMEOUT, (
        "명령 타임아웃이 없으면 먹통 Redis에서 enqueue가 요청을 통째로 멈춘다 (ADR 0005)"
    )
    assert fake.connection_pool.reset_called, (
        "기동 핑에 쓴 커넥션은 타임아웃 없이 만들어졌다 — 버리지 않으면 그 커넥션엔 안 걸린다"
    )
    monkeypatch.setattr(queue_mod, "_queue", None)


def test_prod_refuses_leftover_celery_env(monkeypatch):
    """이름을 바꾸면서 옛 변수를 조용히 무시하면 배송이 재시도 없는 인라인으로 영구 강등된다.

    `extra="ignore"`라 `CELERY_ENABLED=true`만 남아 있으면 `WORKER_ENABLED`가 기본값 False로
    떨어지고, 겉보기엔 푸시가 나가므로 아무도 모른다 — ADR 0018이 "워커가 죽었는데 아무도
    모르는 상태"라 부른 그것이다. 기동을 막아 알린다.
    """
    from app.core.config import validate_settings_for_environment

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setenv("CELERY_ENABLED", "true")

    with pytest.raises(ValueError, match="CELERY_ENABLED"):
        validate_settings_for_environment()
