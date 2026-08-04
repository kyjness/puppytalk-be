"""주기 잡 분산 락 단위 테스트.

운영 전제상 인스턴스가 3~10대다. 락이 없으면 같은 정리 잡이 인스턴스 수만큼 동시에 돌며
같은 청크를 두고 경합한다. 락을 잡 구현이 아니라 **러너**가 거는 것이 이 설계의 핵심 —
잡마다 각자 걸게 두면 빠뜨리기 쉽고 실제로 두 개를 빠뜨렸다.
"""

import asyncio
from typing import cast

from app.core.cleanup import PeriodicJob, job_lock_key, run_jobs_once
from app.infra.redis import RedisLike

from tests.unit.fakes import FakeRedis


def _job(name: str, calls: list[str], *, boom: bool = False) -> PeriodicJob:
    async def run(_task_id: str) -> int:
        calls.append(name)
        if boom:
            raise RuntimeError("job exploded")
        return 1

    return PeriodicJob(name, run)


def _run(jobs, redis) -> None:
    asyncio.run(run_jobs_once(jobs, redis=cast(RedisLike, redis)))


def test_job_runs_and_releases_lock():
    calls: list[str] = []
    redis = FakeRedis()

    _run([_job("purge", calls)], redis)

    assert calls == ["purge"]
    # 해제되지 않으면 TTL 만료까지 전 인스턴스가 skip한다.
    assert job_lock_key("purge") not in redis.kv


def test_job_skipped_when_lock_already_held():
    """다른 인스턴스가 점유 중이면 실행하지 않는다."""
    calls: list[str] = []
    redis = FakeRedis(preloaded={job_lock_key("purge"): "other-instance-token"})

    _run([_job("purge", calls)], redis)

    assert calls == []
    assert redis.kv[job_lock_key("purge")] == "other-instance-token"  # 남의 락 미삭제


def test_lock_released_even_when_job_raises():
    """잡이 터져도 해제 — 안 그러면 한 번의 실패가 TTL 동안 전 인스턴스를 막는다."""
    calls: list[str] = []
    redis = FakeRedis()

    _run([_job("boom", calls, boom=True)], redis)

    assert calls == ["boom"]
    assert job_lock_key("boom") not in redis.kv


def test_one_job_lock_does_not_block_others():
    """잡별로 키가 갈린다 — 한 잡이 점유돼도 나머지는 돈다."""
    calls: list[str] = []
    redis = FakeRedis(preloaded={job_lock_key("held"): "token"})

    _run([_job("held", calls), _job("free", calls)], redis)

    assert calls == ["free"]


def test_redis_absent_runs_without_lock():
    """단일 노드 개발 모드 — Redis 없이도 잡은 돌아야 한다."""
    calls: list[str] = []

    asyncio.run(run_jobs_once([_job("purge", calls)], redis=None))

    assert calls == ["purge"]


def test_redis_failure_falls_open_to_running():
    """락 조회 실패로 정리 잡이 멈추면 안 된다 — 중복 실행이 미실행보다 낫다."""
    calls: list[str] = []

    class _BrokenRedis(FakeRedis):
        async def set(self, key, val, nx=False, ex=None):
            raise ConnectionError("redis down")

    _run([_job("purge", calls)], _BrokenRedis())

    assert calls == ["purge"]
