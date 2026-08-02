# 주기 백그라운드 잡 러너. 어떤 잡을 어떤 보존 정책으로 돌릴지는 main lifespan이 소유하고,
# core는 실행 루프(간격·잡 간 오류 격리·task_id 로그 상관관계)만 제공한다 — core가 도메인
# 서비스를 직접 import하지 않는다. HTTP request가 없으므로 실행마다 task_id(ULID)를 발급.
import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import NamedTuple

from app.core.ids import new_ulid_str

log = logging.getLogger(__name__)


class PeriodicJob(NamedTuple):
    """name은 로그 이벤트 접두사({name}_done/{name}_failed). run은 task_id를 받아 처리 건수를 반환."""

    name: str
    run: Callable[[str], Awaitable[int]]


async def run_jobs_once(jobs: Sequence[PeriodicJob]) -> None:
    """잡을 순차 실행. 한 잡의 실패가 다음 잡을 막지 않는다(경고 로그 후 계속)."""
    task_id = new_ulid_str()
    log.info("cleanup_start task_id=%s", task_id)
    for job in jobs:
        try:
            count = await job.run(task_id)
            if count:
                log.info("%s_done task_id=%s deleted_count=%s", job.name, task_id, count)
        except Exception as e:
            log.warning("%s_failed task_id=%s error=%s", job.name, task_id, e)


async def run_periodic(
    jobs: Sequence[PeriodicJob],
    stop_event: asyncio.Event,
    interval_seconds: float,
) -> None:
    while not stop_event.is_set():
        await run_jobs_once(jobs)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass  # Intended: interval elapsed, run cleanup again
