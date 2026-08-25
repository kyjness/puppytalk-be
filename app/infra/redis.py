# Redis 연결. Rate Limit·Refresh Token 저장. 앱 lifespan에서 init/close.
import hashlib
import logging
import math
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from redis.asyncio import ConnectionPool, Redis
from redis.exceptions import NoScriptError

from app.core.config import settings

if TYPE_CHECKING:
    from arq.connections import RedisSettings

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


def get_websocket_redis(websocket: Any) -> RedisLike | None:
    """WebSocket scope 경유 조회 — Request가 없는 WS 핸들러용 get_app_redis 형제."""
    return get_app_redis(websocket.scope.get("app"))


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


def redis_connection_kwargs(*, socket_timeout: float | None = None) -> dict[str, Any]:
    """모든 Redis 클라이언트가 공유하는 연결 옵션.

    생성부가 넷이라(앱 풀·구독 소켓·워커 멱등 클라이언트·arq 큐) 각자 손으로 쓰면 그중 하나만
    옵션이 빠지는 식으로 조용히 갈라진다 — 실제로 한 번 그렇게 됐다. 여기가 유일한 출처다.
    arq는 자체 `RedisSettings`를 쓰므로 이 dict를 직접 못 받는다 — 아래 `arq_redis_settings()`가
    여기서 값을 꺼내 변환한다(`tests/unit/test_worker_queue_config.py`가 그 경유를 검사한다).

    소켓 타임아웃은 성능 튜닝이 아니라 **fail-open 계약의 전제**다(ADR 0005). 앱의 모든
    fail-open은 `except`로 발동하는데, 타임아웃이 없으면 Redis가 먹통일 때 예외 자체가
    발생하지 않아 rate limit 미들웨어·인증 캐시에서 전 요청이 무한 대기한다.

    `socket_timeout`만 호출부가 덮는다 — 구독 소켓은 유휴가 정상이라 더 길어야 한다
    (`app/infra/pubsub.py::_subscriber_socket_timeout`).
    """
    return {
        "decode_responses": True,
        # `or`가 아니라 `is None` — 0을 조용히 기본값으로 바꿔치기하지 않는다.
        "socket_timeout": (
            settings.REDIS_SOCKET_TIMEOUT if socket_timeout is None else socket_timeout
        ),
        "socket_connect_timeout": settings.REDIS_SOCKET_CONNECT_TIMEOUT,
    }


def arq_redis_settings() -> "RedisSettings | None":
    """워커 큐(arq)용 연결 설정. `REDIS_URL`이 비면 None(큐 비활성).

    arq는 redis-py 클라이언트를 자기 `RedisSettings`로 직접 만든다 — 위 `redis_connection_kwargs`를
    안 거치는 **네 번째 생성부**다. 그대로 두면 ADR 0005가 막으려던 "생성부마다 타임아웃이 갈린다"가
    재발하므로, 변환을 이 모듈 안에 가둬 창구를 하나로 유지한다.

    `ARQ_REDIS_DB`로 앱 데이터와 DB 인덱스를 분리한다(큐 키가 앱 키스페이스를 오염시키지 않게).
    arq에는 명령 단위 `socket_timeout`이 없고 연결 타임아웃(`conn_timeout`)만 있다 — 그 차이는
    ADR 0020 트레이드오프에 적었다.
    """
    from arq.connections import RedisSettings

    if not settings.REDIS_URL:
        return None
    rs = RedisSettings.from_dsn(settings.REDIS_URL)
    rs.database = settings.ARQ_REDIS_DB
    # 타임아웃 값은 위 공용 창구에서 꺼낸다 — 설정을 직접 읽으면 읽는 곳이 둘이 되어
    # "유일한 출처"가 문구로만 남는다.
    #
    # arq의 conn_timeout은 int다. 내림하면 설정값보다 **짧아져** 정상 연결이 오탐으로 끊길 수
    # 있으므로 올림한다 — 타임아웃은 늘어나는 쪽이 안전하다.
    rs.conn_timeout = math.ceil(redis_connection_kwargs()["socket_connect_timeout"])
    # arq 기본은 5회 재시도 × 1초 지연이다. `conn_retries`는 `create_pool`에서만 읽히고
    # 클라이언트로 전달되지 않으므로 **요청 경로에는 닿지 않는다** — 요청 경로 방어는
    # lifespan에서 풀을 미리 만드는 쪽이 혼자 맡는다(ADR 0020 결정 5).
    # 여기서 자르는 것은 **부팅 지연**이다: 죽은 Redis에서 기본값이면 ~17초, 이 값이면 ~4초.
    # 지연도 함께 없앤다 — 재시도를 1회로 줄여도 그 사이 1초 슬립은 arq 기본으로 남는다.
    rs.conn_retries = 1
    rs.conn_retry_delay = 0
    # 이 레포의 다른 클라이언트는 전부 풀 크기를 의도적으로 묶는다(앱 128 · 워커 멱등 4).
    # arq 기본은 None이고 redis-py가 그걸 2**31로 바꾼다 — 사실상 무제한이라 2GB 박스에서
    # 앱 풀 128 위로 끝없이 얹힌다. enqueue는 밀리초 단위라 16이면 넉넉하다.
    rs.max_connections = 16
    return rs


def create_redis_client(*, max_connections: int | None = None) -> RedisLike | None:
    """설정에서 풀 클라이언트를 만든다(연결 확인은 호출부). REDIS_URL이 비면 None.

    풀 크기만 호출부가 정한다 — 앱 lifespan은 설정값(SSE pubsub이 길게 점유), 워커는 작게.
    풀 옵션(`ConnectionPool.from_url` 인자)의 출처는 여기 하나다.
    """
    if not settings.REDIS_URL:
        return None
    pool = ConnectionPool.from_url(
        settings.REDIS_URL,
        max_connections=(
            settings.REDIS_MAX_CONNECTIONS if max_connections is None else max_connections
        ),
        **redis_connection_kwargs(),
    )
    # RedisLike 주석은 실클라이언트가 Protocol 계약을 만족하는지 타입 수준에서 강제한다.
    client: RedisLike = Redis(connection_pool=pool)
    return client


async def init_redis(app) -> None:
    app.state.redis = None
    if not settings.REDIS_URL:
        return
    try:
        client = create_redis_client()
        app.state.redis = client
        if client is None:
            return
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
