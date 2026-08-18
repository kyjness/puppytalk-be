"""Redis "먹통" 모드에서 fail-open이 실제로 발동하는지 (ADR 0003·0004·0005).

Redis가 **죽은 것**(프로세스 down → connection refused)과 **먹통인 것**(네트워크 분단·
보안그룹 차단·ElastiCache 페일오버 → 패킷이 조용히 사라짐)은 다른 실패 모드다.
코드베이스의 fail-open은 전부 `try/except` 기반이라 **예외가 와야** 발동하는데,
소켓 타임아웃이 없으면 먹통 모드에서는 예외가 영원히 오지 않는다.

그래서 가짜 Redis로는 이 계약을 증명할 수 없다 — 가짜는 즉시 반환하거나 즉시 던질 뿐,
"매달린다"를 재현하지 못한다. 실제로 accept만 하고 응답하지 않는 소켓이 필요하다.

단언은 구현 상수(`socket_timeout == 1.0`)가 아니라 **사용자 층위**에 둔다 — 값을 못박으면
그 값 자체가 계약이 되어 튜닝할 때마다 테스트를 고쳐야 하고, 정작 "무한 대기하지 않는다"는
본래 계약은 검증되지 않는다. 같은 이유로 테스트는 프로덕션 기본값(1s·5s)을 그대로
기다리지 않고 **자기 타임아웃을 주입한다** — 기다림은 스위트 시간일 뿐 계약이 아니다.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable
from typing import Any, TypeVar
from uuid import UUID

import pytest
from app.core.config import settings
from app.core.rate_limit import check_fixed_window
from app.infra.cache import get_or_compute_json
from app.infra.redis import RedisLike, create_redis_client
from pydantic import TypeAdapter

pytestmark = pytest.mark.asyncio

# "유한한가"만 본다. 정확한 소요 시간을 단언하면 타임아웃 값을 얼리게 되므로 넉넉히 잡는다
# (재연결 재시도가 끼어도 통과할 만큼). 수정 전 코드는 무한 대기라 어떤 값을 줘도 실패한다.
# 통과 경로에서는 지불되지 않는다 — 아래 _TEST_SOCKET_TIMEOUT 참조.
_BUDGET_SEC = 10.0

# 테스트가 실제로 기다리는 시간. 프로덕션 기본값(1s)을 그대로 쓰면 이 파일 하나가
# 단위 스위트에 수 초를 얹는데, 그 초는 아무것도 증명하지 않는다.
_TEST_SOCKET_TIMEOUT = 0.1

_T = TypeVar("_T")


async def _within_budget(awaitable: Awaitable[_T], why: str) -> _T:
    """예산 안에 끝나지 않으면 "행"이 아니라 **읽히는 실패**로 떨어뜨린다."""
    try:
        return await asyncio.wait_for(awaitable, timeout=_BUDGET_SEC)
    except TimeoutError:
        pytest.fail(f"{_BUDGET_SEC}s 안에 {why} — Redis 소켓 타임아웃 부재.")


@contextlib.asynccontextmanager
async def _blackhole_redis() -> AsyncIterator[str]:
    """TCP accept는 하고 한 바이트도 응답하지 않는 리스너의 redis:// URL.

    연결 자체는 성립하므로(핸드셰이크 완료) connect timeout이 아니라 **read timeout**
    경로를 탄다 — redis-py가 접속 직후 보내는 HELLO/AUTH 응답을 기다리며 매달린다.
    이것이 운영에서 실제로 무서운 쪽이다. 죽은 포트(connection refused)는 즉시 예외가 나
    기존 fail-open이 이미 덮고 있다.
    """

    async def _never_reply(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # 클라이언트가 끊을 때까지(EOF) 읽기만 하고 아무것도 쓰지 않는다. Event().wait()로
        # 매달리면 핸들러가 끝나지 않아 Python 3.12+의 wait_closed()가 영원히 대기한다.
        try:
            await reader.read()
        finally:
            writer.close()

    server = await asyncio.start_server(_never_reply, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"redis://127.0.0.1:{port}/0"
    finally:
        server.close()
        # 타임아웃이 없는(=결함이 남은) 코드에서는 클라이언트가 끊지 않아 핸들러가 살아 있다.
        # 정리에서까지 매달리면 실패가 "테스트 행"으로 보여 원인을 가린다.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(server.wait_closed(), timeout=2.0)


@contextlib.asynccontextmanager
async def _client_to_blackhole(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[RedisLike]:
    async with _blackhole_redis() as url:
        monkeypatch.setattr(settings, "REDIS_URL", url)
        monkeypatch.setattr(settings, "REDIS_SOCKET_TIMEOUT", _TEST_SOCKET_TIMEOUT)
        client = create_redis_client()
        assert client is not None
        try:
            yield client
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.aclose(), timeout=2.0)


@pytest.mark.parametrize(
    "fail_open, key, max_count",
    [(True, "global:blackhole", 10), (False, "login:blackhole", 5)],
    ids=["fail_open", "memory_fallback"],
)
async def test_rate_limit_stays_bounded_when_redis_is_unresponsive(
    monkeypatch: pytest.MonkeyPatch, fail_open: bool, key: str, max_count: int
) -> None:
    """rate limit은 먹통 Redis에서 **유한 시간 안에** 통과해야 한다.

    - `fail_open=True`(전역 한도): 그냥 통과. ASGI 미들웨어라 요청이 핸들러에 닿기 전에
      지나므로, 여기서 매달리면 Redis 하나 때문에 전 요청이 멈춘다(ADR 0005).
    - `fail_open=False`(남용 방어): 인메모리 폴백으로 넘어간다. ADR 0003이 "Redis가
      죽었다고 로그인이 막히면 안 된다"고 약속한 지점이다.

    둘 다 폴백이 `except`로 발동하므로 타임아웃이 없으면 한 줄도 실행되지 않는다.
    """
    async with _client_to_blackhole(monkeypatch) as client:
        allowed, _ = await _within_budget(
            check_fixed_window(
                client, key, window_sec=60, max_count=max_count, fail_open=fail_open
            ),
            "폴백이 발동하지 않았다 — 먹통 Redis에서 요청이 미들웨어에 갇힌다",
        )
        assert allowed is True  # 첫 요청이므로 로컬 윈도에서도 통과


async def test_cache_falls_back_to_loader_in_bounded_time_when_redis_is_unresponsive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """캐시는 먹통 Redis에서 **유한 시간 안에 loader(DB)로 폴백**해야 한다(ADR 0004 결정 2).

    `get_current_user`가 매 인증 요청마다 이 계층을 지나므로, 여기서 매달리면
    인증된 요청 전체가 멈춘다.
    """
    loaded: list[int] = []

    async def _loader() -> list[int]:
        loaded.append(1)
        return [1, 2, 3]

    async with _client_to_blackhole(monkeypatch) as client:
        result = await _within_budget(
            get_or_compute_json(
                redis=client,
                key="test:blackhole",
                lock_key="test:blackhole:lock",
                ttl_seconds=60,
                adapter=TypeAdapter(list[int]),
                loader=_loader,
                cache_name="blackhole_test",
            ),
            "loader 폴백이 일어나지 않았다",
        )
        assert result == [1, 2, 3]
        assert loaded == [1], "폴백이 loader를 정확히 한 번 호출해야 한다"


async def test_pubsub_listener_raises_in_bounded_time_when_redis_is_unresponsive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """구독 리스너는 먹통 Redis에서 **예외를 던져** 백오프 재연결로 넘어가야 한다.

    `_listen_once`의 계약은 "연결·수신 계층 예외는 밖으로 던져 재연결을 유도한다"인데,
    타임아웃이 없으면 접속·구독 단계에서 매달려 그 예외가 영영 오지 않는다. 그러면
    이 인스턴스의 크로스 인스턴스 실시간 전달이 프로세스 재시작까지 조용히 죽는다 —
    재연결 기계가 있는데도 돌지 않는, 코드만 봐서는 안 보이는 실패다.
    """
    from app.infra import pubsub as pubsub_mod

    async def _noop(_user_id: UUID, _payload: str) -> None:
        return None

    # 구독 타임아웃은 `max(설정, 폴 간격 * 5)`라 **둘 다** 낮춰야 실제로 짧아진다.
    monkeypatch.setattr(settings, "REDIS_SOCKET_TIMEOUT", _TEST_SOCKET_TIMEOUT)
    monkeypatch.setattr(pubsub_mod, "_MESSAGE_POLL_TIMEOUT_SEC", _TEST_SOCKET_TIMEOUT / 2)

    async with _blackhole_redis() as url:
        try:
            await _within_budget(
                pubsub_mod._listen_once(
                    redis_url=url,
                    handlers={"test-channel": _noop},
                    stop_event=asyncio.Event(),
                    on_healthy=lambda: None,
                ),
                "예외가 오지 않았다 — 백오프 재연결이 발동하지 못한다",
            )
        except Exception:
            return  # 계약대로 연결 계층 예외가 올라왔다
        pytest.fail("먹통 Redis인데 _listen_once가 정상 종료했다")


# --- 배선 그물 ---
# 위 동작 테스트가 본 계약이고, 아래는 클라이언트 생성부가 셋이라 그중 하나만 옵션이
# 빠지는 것을 막는 값싼 보조 검사다. 셋 다 `redis_connection_kwargs()`를 거쳐야 한다.


def _app_client() -> Any:
    return create_redis_client()


def _worker_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    from app.worker.jobs import notification_delivery as worker_mod

    monkeypatch.setattr(worker_mod, "_redis_client", None)  # 프로세스 캐시 우회
    return worker_mod._get_redis()


def _subscriber_client() -> Any:
    from app.infra import pubsub as pubsub_mod

    return pubsub_mod.make_subscriber_client(settings.REDIS_URL)


@pytest.mark.parametrize(
    "make", ["app", "worker", "subscriber"], ids=["app", "worker", "subscriber"]
)
async def test_every_redis_client_carries_socket_timeouts(
    monkeypatch: pytest.MonkeyPatch, make: str
) -> None:
    """앱 풀·워커·구독 소켓 셋 다 타임아웃을 실어야 한다.

    워커가 빠지면 먹통 Redis에서 멱등성 조회가 워커 슬롯을 소진하고, 구독 소켓이 빠지면
    그 인스턴스의 실시간 전달이 재시작까지 죽는다 — 셋의 실패 결과가 각각 다르다.
    """
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:6379/0")
    client = {
        "app": _app_client,
        "worker": lambda: _worker_client(monkeypatch),
        "subscriber": _subscriber_client,
    }[make]()
    assert client is not None

    kwargs = client.connection_pool.connection_kwargs
    # 구독 소켓만 폴 간격 하한이 걸려 값이 크다 — "설정 이상"이면 계약을 지킨 것이다.
    assert kwargs["socket_timeout"] >= settings.REDIS_SOCKET_TIMEOUT
    assert kwargs["socket_connect_timeout"] == settings.REDIS_SOCKET_CONNECT_TIMEOUT
