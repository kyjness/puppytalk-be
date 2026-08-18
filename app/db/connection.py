# DB 연결 수명 주기. init_database(시작 시 재시도), check_database, close_database. 비동기.
import asyncio
import logging

from sqlalchemy import text

from app.core.config import settings
from app.db.engine import reader_engine, writer_engine

logger = logging.getLogger(__name__)


async def _ping(engine) -> None:
    """엔진 하나에 SELECT 1. 유한 시간 안에 끝나야 한다.

    `DB_PING_TIMEOUT`은 `app/db/engine.py`에서 `connect_timeout`으로도 쓰이지만 그건
    **연결 수립**만 덮는다 — 풀에 이미 있는 커넥션을 재사용하면 적용되지 않으므로,
    DB가 *접속은 되는데 응답만 멎은* 상태에서 쿼리가 무한 대기한다. 그러면 `/readyz`가
    503 대신 매달리고(프로브 타임아웃으로 원인이 가려진다) 호출마다 커넥션을 붙잡아
    풀을 잠식한다. 여기서 상한을 걸어 실패를 **빠르고 명확하게** 만든다.
    """
    async with engine.connect() as conn:
        await asyncio.wait_for(conn.execute(text("SELECT 1")), timeout=settings.DB_PING_TIMEOUT)


async def _try_connect() -> tuple[bool, Exception | None]:
    try:
        await _ping(writer_engine)
        await _ping(reader_engine)
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
