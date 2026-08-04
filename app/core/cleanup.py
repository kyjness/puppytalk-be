# 주기 백그라운드 잡 러너. 어떤 잡을 어떤 보존 정책으로 돌릴지는 main lifespan이 소유하고,
# core는 실행 루프(간격·잡 간 오류 격리·task_id 로그 상관관계·분산 락)만 제공한다 — core가
# 도메인 서비스를 직접 import하지 않는다. HTTP request가 없으므로 실행마다 task_id(ULID)를 발급.
import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import NamedTuple

from app.core.ids import new_ulid_str
from app.infra.lock import release_lock, try_acquire_job_lock
from app.infra.redis import RedisLike

log = logging.getLogger(__name__)

# 잡 락 기본 TTL. 잡이 이보다 오래 돌면 락이 만료돼 다른 인스턴스가 끼어들 수 있으므로,
# 오래 도는 잡은 PeriodicJob.lock_ttl_seconds로 개별 상향한다.
DEFAULT_JOB_LOCK_TTL_SECONDS = 600


def job_lock_key(name: str) -> str:
    return f"lock:job:{name}"


class PeriodicJob(NamedTuple):
    """name은 로그 이벤트 접두사({name}_done/{name}_failed)이자 잡 락 키의 근거.

    run은 task_id를 받아 처리 건수를 반환한다.
    """

    name: str
    run: Callable[[str], Awaitable[int]]
    lock_ttl_seconds: int = DEFAULT_JOB_LOCK_TTL_SECONDS


async def run_jobs_once(jobs: Sequence[PeriodicJob], *, redis: RedisLike | None = None) -> None:
    """잡을 순차 실행. 한 잡의 실패가 다음 잡을 막지 않는다(경고 로그 후 계속).

    락은 **잡별로 러너가 건다** — 잡 구현이 각자 걸게 두면 빠뜨리기 쉽고 실제로 빠뜨렸다.
    운영 전제상 인스턴스가 3~10대이므로, 락이 없으면 같은 정리 잡이 인스턴스 수만큼 동시에
    돌며 같은 청크를 두고 경합한다. Redis 부재·장애 시 정책은 try_acquire_job_lock 참조.
    """
    task_id = new_ulid_str()
    log.info("cleanup_start task_id=%s", task_id)
    for job in jobs:
        acquired, token = await try_acquire_job_lock(
            redis, key=job_lock_key(job.name), ttl_seconds=job.lock_ttl_seconds
        )
        if not acquired:
            log.info("%s_skipped task_id=%s reason=lock_held", job.name, task_id)
            continue
        try:
            count = await job.run(task_id)
            if count:
                log.info("%s_done task_id=%s deleted_count=%s", job.name, task_id, count)
        except Exception as e:
            log.warning("%s_failed task_id=%s error=%s", job.name, task_id, e)
        finally:
            # 잡이 예외로 끝나도 반드시 해제 — 안 그러면 TTL 만료까지 전 인스턴스가 skip한다.
            if token is not None and redis is not None:
                await release_lock(redis, job_lock_key(job.name), token)


async def run_periodic(
    jobs: Sequence[PeriodicJob],
    stop_event: asyncio.Event,
    interval_seconds: float,
    *,
    redis: RedisLike | None = None,
) -> None:
    while not stop_event.is_set():
        await run_jobs_once(jobs, redis=redis)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass  # Intended: interval elapsed, run cleanup again
