# arq 워커 진입점. 실행: `arq app.worker.settings.WorkerSettings` (poe worker).

import logging
from typing import Any

from app.core.config import settings
from app.db import close_database, init_database
from app.infra.redis import arq_redis_settings
from app.worker.jobs.notification_delivery import deliver_notification_sns

log = logging.getLogger(__name__)


async def on_startup(ctx: dict[str, Any]) -> None:
    """워커 프로세스의 사전 조건을 확인하고 async DB 풀을 연다.

    Celery 시절엔 동기 워커라 프로세스당 이벤트 루프를 손으로 유지하는 어댑터
    (`async_bridge`)가 필요했다. arq는 자체 루프에서 도는 async 워커라 여기서 그냥 await한다.

    여기서 raise하면 `Worker.main`이 poll 루프 **전에** 죽는다(try/except 없이 await된다) —
    잡을 하나도 집지 않고 종료하므로 사전 조건 검사 자리로 맞다.
    """
    # arq의 `Worker.__init__`은 `redis_settings or RedisSettings()`라, 설정이 None이면 조용히
    # **localhost:6379/db 0**으로 붙는다. db 0은 앱 데이터 키스페이스라, 큐 키를 분리하려고 둔
    # ARQ_REDIS_DB가 무의미해지고 설정 누락이 "붙긴 붙었는데 엉뚱한 곳"으로 넘어간다.
    #
    # 발행측(app/infra/queue.py)은 같은 상황에서 None을 반환해 인라인 폴백으로 떨어진다 —
    # 앱은 열화된 채로 서빙하는 게 맞지만(fail-open, ADR 0005), 워커가 엉뚱한 Redis를 잡는 것은
    # 열화가 아니라 오염이다. 그래서 발행측은 fail-open, 워커는 fail-fast로 갈린다.
    if WorkerSettings.redis_settings is None:
        raise RuntimeError(
            "arq 워커: REDIS_URL(또는 ARQ_REDIS_URL)이 비어 있다. "
            "그대로 두면 arq가 조용히 localhost:6379/0(앱 키스페이스)으로 붙는다."
        )
    log.info("arq_worker_startup: initializing async DB pools")
    if not await init_database():
        raise RuntimeError("arq worker: PostgreSQL connection failed")


async def on_shutdown(ctx: dict[str, Any]) -> None:
    log.info("arq_worker_shutdown: disposing async DB pools")
    await close_database()


class WorkerSettings:
    """큐 하나·잡 하나. 등급 3(재시도가 유일한 복구)에 해당하는 작업만 여기 온다(ADR 0018·0020).

    Celery 시절의 `default`/`high_priority` 2큐는 잡이 하나뿐이라 나눌 대상이 없었다 —
    라우팅 표면을 만들지 않는다. 필요해지면 그때 `queue_name`을 나눈다.
    """

    functions = [deliver_notification_sns]
    # None일 수 있다 — 그 경우를 막는 것은 on_startup의 사전 조건 검사다(위 주석 참조).
    # 여기서 raise하면 Redis 없이 이 모듈을 import 하는 것조차 불가능해진다.
    redis_settings = arq_redis_settings()
    on_startup = on_startup
    on_shutdown = on_shutdown

    max_tries = settings.ARQ_MAX_TRIES
    job_timeout = settings.ARQ_JOB_TIMEOUT
    # 결과를 읽는 곳이 없다. Celery는 이걸 끄지 않으면 enqueue가 결과 백엔드를 pubsub subscribe
    # 하는 위험이 있었지만(구 test_celery_config), arq는 결과를 Redis 키로만 남기므로 보관 기간을
    # 0으로 두는 것으로 끝난다 — 같은 목적이 설정 한 줄로 해결된다.
    keep_result = 0
    # 잡이 하나이고 데모는 단일 인스턴스다. Celery의 --concurrency=2와 같은 수준.
    max_jobs = 2
    # arq 기본 0.5s면 유휴에도 초당 2회 ZRANGEBYSCORE = 하루 17만 회다. 같은 compose 파일이
    # 워커 헬스체크를 "30초마다 Redis 왕복은 2GB에서 과하다"며 거부하는데, 그보다 60배 비싸다.
    # 오프라인 푸시에 sub-second SLA는 없다 — 인앱 실시간은 이미 전달된 뒤다.
    poll_delay = 2
    # 기본은 max(max_jobs*5, 100)=100이라 100건을 읽어 2건만 쓰고 버린다.
    queue_read_limit = 4
