# DB 연결 수명 주기. init_database(시작 시 재시도), check_database, close_database. 비동기.
import asyncio
import logging
from collections.abc import Awaitable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import settings
from app.db.engine import reader_engine, writer_engine

logger = logging.getLogger(__name__)


async def _ping(engine: AsyncEngine) -> None:
    """엔진 하나에 checkout + SELECT 1. `_bounded`가 상한을 건다.

    `pool_pre_ping`의 `SELECT 1`은 `engine.connect()` **안**에서 돌므로 상한은 execute만이
    아니라 이 함수 전체에 걸려야 한다 — 그래서 여기엔 타임아웃이 없고 호출부가 감싼다.
    """
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def _bounded(coro: Awaitable[None], timeout: float) -> None:
    """프로브 하나를 유한 시간 안에 끝낸다 — 실제 드라이버 동작까지 감안해서.

    엔진의 `connect_timeout`은 **연결 수립**만 덮으므로, 풀 커넥션을 재사용하면 응답만 멎은
    DB에서 쿼리가 무한 대기한다 — `/readyz`가 503 대신 매달리고 호출마다 커넥션을 붙잡아
    풀을 잠식한다. 그런데 `asyncio.wait_for` 한 번으로는 부족하다: psycopg 3.x는 첫
    `CancelledError`를 **잡아서** 서버에 취소를 보낸 뒤 같은 쿼리를 다시 기다린다
    (`AsyncConnection.wait`). 먹통 서버에선 그 재대기도 영영 안 끝난다. 두 번째 취소는
    잡지 않으므로 **두 번 보내야** 실제로 끝난다.

    요청 경로의 일반 쿼리에는 이 상한이 없다 — 프로브만 자체 상한을 가진다(전역
    `statement_timeout`은 장기 배치·마이그레이션과 충돌하는 별도 결정).
    """
    task = asyncio.ensure_future(coro)
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if task in done:
        task.result()  # 예외가 있으면 여기서 올라온다
        return
    for _ in range(2):
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if task in done:
            break
    raise TimeoutError(f"DB probe exceeded {timeout}s")


async def _try_connect() -> tuple[bool, Exception | None]:
    try:
        await _bounded(_ping(writer_engine), settings.DB_PING_TIMEOUT)
        await _bounded(_ping(reader_engine), settings.DB_PING_TIMEOUT)
        return True, None
    except Exception as e:
        return False, e


async def check_database() -> bool:
    ok, err = await _try_connect()
    if not ok and err is not None:
        logger.error("PostgreSQL 연결 실패: %s", err)
    return ok


async def init_database() -> bool:
    max_attempts = settings.DB_INIT_MAX_ATTEMPTS
    delay = max(0.0, settings.DB_INIT_RETRY_DELAY_SECONDS)
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        ok, err = await _try_connect()
        if ok:
            return True
        last_err = err
        if attempt < max_attempts:
            logger.warning(
                "PostgreSQL 연결 실패 (%s/%s): %s — %.1fs 후 재시도",
                attempt,
                max_attempts,
                err,
                delay,
            )
            if delay > 0:
                await asyncio.sleep(delay)
    if last_err is not None:
        logger.error("PostgreSQL 연결 실패 (최종, %s회 시도): %s", max_attempts, last_err)
    return False


async def close_database() -> None:
    await writer_engine.dispose()
    await reader_engine.dispose()
