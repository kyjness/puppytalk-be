# Redis 연결. Rate Limit·Refresh Token 저장. 앱 lifespan에서 init/close.
import hashlib
import logging
from collections.abc import Awaitable
from typing import Any, Protocol, runtime_checkable

from redis.asyncio import ConnectionPool, Redis
from redis.exceptions import NoScriptError

from app.core.config import settings

log = logging.getLogger(__name__)


@runtime_checkable
class RedisLike(Protocol):
    """앱이 사용하는 Redis 명령의 구조적 계약 — isinstance 가드는 혈통(실클라이언트
    상속)이 아니라 이 능력 집합을 검사한다. 테스트 가짜가 Redis를 상속할 필요가
    없어져, 상속 시그니처 충돌을 가리던 로컬 스텁(typings/) 없이 업스트림 타입
    그대로 검사받는다.

    파라미터는 positional-only(/)로 선언해 redis-py의 파라미터 이름(name=…)과의
    표기 차이를 계약에서 배제하고, 반환은 Any — redis-py 명령이 동기/비동기 겸용
    유니온(`Awaitable[T] | T`)을 반환해 좁은 반환 타입은 실클라이언트와 어긋난다.
    호출부는 항상 await한다. 멤버는 실사용 명령만 — 넓힐 때는 호출부 추가와
    함께 여기에 등록한다(runtime_checkable isinstance는 멤버 수에 비례).
    """

    def ping(self) -> Any: ...
    def aclose(self) -> Awaitable[None]: ...
    def get(self, key: str, /) -> Any: ...
    def set(self, key: str, value: Any, /, *, nx: bool = ..., ex: int | None = ...) -> Any: ...
    def setex(self, key: str, seconds: int, value: Any, /) -> Any: ...
    def delete(self, *keys: str) -> Any: ...
    def eval(self, script: str, numkeys: int, /, *args: Any) -> Any: ...
    def evalsha(self, sha: str, numkeys: int, /, *args: Any) -> Any: ...
    def hget(self, key: str, field: str, /) -> Any: ...
    def hgetall(self, key: str, /) -> Any: ...
    def hincrby(self, key: str, field: str, amount: int, /) -> Any: ...
    def publish(self, channel: str, message: str, /) -> Any: ...


def get_app_redis(app: Any) -> RedisLike | None:
    """앱 lifespan에 붙은 클라이언트 조회의 단일 창구(핫패스 — rate limit이 매 요청 호출).

    RedisLike isinstance 검사는 부팅·종료(init_redis/close_redis)에서만 수행한다 —
    runtime_checkable Protocol isinstance는 멤버 수에 비례하는 속성 검사라 매 요청
    태우기엔 비싸고, state에 실리는 값은 init_redis가 이미 계약을 강제한 클라이언트뿐이다.
    """
    return getattr(app.state, "redis", None) if app is not None else None


async def eval_script_cached(
    redis: RedisLike, script: str, sha: str, numkeys: int, /, *args: Any
) -> Any:
    """EVALSHA 우선 실행 — 매 호출 스크립트 전문 전송을 피한다(핫패스 왕복 절감).

    서버 스크립트 캐시가 빈 경우(NOSCRIPT — 재시작·FLUSH 직후)만 EVAL로 폴백해
    자동 재로드한다(EVAL이 같은 sha1로 캐시를 채운다). sha는 호출부가
    hashlib.sha1(script)로 사전 계산해 상수로 둔다."""
    try:
        return await redis.evalsha(sha, numkeys, *args)
    except NoScriptError:
        return await redis.eval(script, numkeys, *args)


_RENAME_IF_EXISTS_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return 0
end
redis.call('RENAME', KEYS[1], KEYS[2])
return 1
"""
_RENAME_IF_EXISTS_SHA = hashlib.sha1(_RENAME_IF_EXISTS_LUA.encode()).hexdigest()


async def rename_if_exists(redis: RedisLike, src: str, dst: str, /) -> bool:
    """src 키가 있으면 dst로 원자적 RENAME. 없으면 no-op(False)."""
    renamed = await eval_script_cached(
        redis, _RENAME_IF_EXISTS_LUA, _RENAME_IF_EXISTS_SHA, 2, src, dst
    )
    return bool(int(renamed))


async def merge_hash_into(redis: RedisLike, src: str, dst: str, /) -> None:
    """src 해시의 정수 필드를 dst 해시에 합산한 뒤 src를 삭제한다(재병합용)."""
    fields = await redis.hgetall(src)
    for field, raw in fields.items():
        await redis.hincrby(dst, field, int(raw))
    await redis.delete(src)


def bulk_to_str(value: Any) -> str | None:
    """Redis GET 결과(bytes/str)를 비교용 문자열로 통일한다."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8")
    return str(value)


async def init_redis(app) -> None:
    app.state.redis = None
    if not settings.REDIS_URL:
        return
    try:
        pool = ConnectionPool.from_url(
            settings.REDIS_URL,
            max_connections=settings.REDIS_MAX_CONNECTIONS,
            decode_responses=True,
        )
        # RedisLike 주석은 실클라이언트가 Protocol 계약을 만족하는지 타입 수준에서 강제한다.
        client: RedisLike = Redis(connection_pool=pool)
        app.state.redis = client
        await client.ping()
        log.info(
            "Redis connection pool initialized (max_connections=%s).",
            settings.REDIS_MAX_CONNECTIONS,
        )
    except Exception as e:
        log.warning("Redis 연결 실패: %s. Rate limit 미들웨어는 Fail-open.", e)
        app.state.redis = None


async def close_redis(app) -> None:
    client = getattr(app.state, "redis", None)
    if isinstance(client, RedisLike):
        # connection_pool은 실클라이언트 전용이라 Protocol 계약 밖 — getattr로 유무만 본다.
        pool = getattr(client, "connection_pool", None)
        await client.aclose()
        if pool is not None:
            await pool.disconnect()
        app.state.redis = None
        log.info("Redis connection closed.")
