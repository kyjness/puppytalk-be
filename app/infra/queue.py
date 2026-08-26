# 발행측(요청 경로) arq 큐 핸들. 잡 실행은 워커 프로세스(app.worker.settings)가 한다.

import logging
from typing import TYPE_CHECKING

from app.core.config import settings
from app.infra.redis import arq_redis_settings, redis_connection_kwargs

if TYPE_CHECKING:
    from arq.connections import ArqRedis

log = logging.getLogger(__name__)

# 프로세스당 하나. 앱 Redis 풀(`app.state.redis`)과 별개다 — arq는 자체 클라이언트를 쓰고
# DB 인덱스도 분리한다. 잡 쪽 `_redis_client`(app/worker/jobs/notification_delivery.py)와
# 같은 패턴이다.
#
# `app.state` + `get_app_*`를 안 쓰는 이유: 소비자인 `NotificationService._dispatch_sns_publish`가
# classmethod라 `app` 핸들이 없다. 다만 형제 인자인 `redis`는 호출부에서 스레딩되어 오므로,
# 큐도 같은 경로로 넘기는 편이 이 레포 관례에 더 맞다 — 호출부 두 곳을 건드려야 해 별도 작업으로 둔다.
_queue: "ArqRedis | None" = None


async def init_queue() -> None:
    """lifespan 기동 시 1회. 실패는 삼킨다 — 큐가 없으면 배송이 인라인으로 떨어진다(fail-open).

    **요청 경로에서 지연 생성하지 않는다.** `create_pool`은 연결 재시도를 하므로 첫 알림 요청이
    죽은 Redis에서 그만큼 매달리게 된다(ADR 0005). 기동 때 한 번 치르고, 실패하면 큐 없이 간다.
    """
    global _queue
    if not settings.WORKER_ENABLED:
        # 조용히 넘어가면 "워커가 죽었는데 아무도 모르는" 상태와 구분이 안 된다 —
        # 배송이 재시도 없는 인라인으로 떨어진다는 사실을 기동 로그에 남긴다(ADR 0018).
        log.warning(
            "arq_queue_disabled: WORKER_ENABLED=false — 배송이 인라인(재시도 없음)으로 떨어진다"
        )
        return
    rs = arq_redis_settings()
    if rs is None:
        log.warning("arq_queue_disabled: REDIS_URL이 비어 있어 배송이 인라인으로 떨어진다")
        return
    try:
        from arq.connections import create_pool

        _queue = await create_pool(rs)
        _apply_command_timeout(_queue)
        log.info("arq_queue_ready db=%s", rs.database)
    except Exception:
        log.exception("arq_queue_init_failed — 배송은 인라인 폴백으로 진행된다")
        _queue = None


def _apply_command_timeout(pool: "ArqRedis") -> None:
    """명령 타임아웃을 풀에 건다 — arq가 안 걸어주는 것을 여기서 메운다.

    `RedisSettings`에는 명령 타임아웃 필드가 없고 `create_pool`은 `conn_timeout`을
    `socket_connect_timeout`으로만 넘긴다. 즉 **연결만 유한하고 명령은 무한**이다.

    이건 ADR 0005가 막으려는 바로 그 구멍이다 — Redis가 *죽은* 게 아니라 *먹통*일 때
    (BGSAVE fork 정지·2GB 박스 스왑 스래싱) TCP는 살아 있어 연결은 성립하고, 보낸 명령의
    응답만 영영 안 온다. `enqueue_job`은 요청 경로(`publish_after_commit`의 gather 안)라
    **예외가 안 나면 인라인 폴백도 발동하지 않고 요청이 통째로 멈춘다.**
    지운 Celery 설정은 이걸 `broker_transport_options={"socket_timeout": 5}`로 막고 있었다.

    `create_pool`이 기동 핑에 쓴 커넥션은 타임아웃 없이 만들어졌으므로 함께 버린다 —
    `connection_kwargs`는 **이후 생성되는** 커넥션에만 적용된다.
    """
    kwargs = pool.connection_pool.connection_kwargs
    kwargs["socket_timeout"] = redis_connection_kwargs()["socket_timeout"]
    pool.connection_pool.reset()


def get_queue() -> "ArqRedis | None":
    """발행용 큐 핸들. None이면 호출부가 인라인 폴백으로 간다."""
    return _queue


async def close_queue() -> None:
    """`close_redis`와 같은 2단계 — aclose()만으로는 풀 커넥션이 남는다."""
    global _queue
    if _queue is None:
        return
    try:
        pool = getattr(_queue, "connection_pool", None)
        await _queue.aclose()
        if pool is not None:
            await pool.disconnect()
    except Exception:
        log.exception("arq_queue_close_failed")
    finally:
        _queue = None
