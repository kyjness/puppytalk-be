"""설정 값 검증 — 잘못된 값이 조용히 "다른 의미"로 굳는 것을 막는다."""

import pytest
from app.core.config import Settings
from pydantic import ValidationError


@pytest.mark.parametrize("field", ["REDIS_SOCKET_TIMEOUT", "REDIS_SOCKET_CONNECT_TIMEOUT"])
@pytest.mark.parametrize("bad", [0, 0.0, -1])
def test_redis_socket_timeouts_must_be_positive(field, bad):
    """0은 "타임아웃 없음"이 아니라 **즉시 타임아웃**이다.

    redis-py는 값이 None이 아니면 `async_timeout(값)`으로 모든 read를 감싸므로 0을 넣으면
    모든 명령이 즉시 실패한다 → 부팅 ping이 실패해 앱이 영구 fail-open(rate limit·인증
    캐시·멱등성 전부 꺼진 채)으로 뜬다. `.env` 주석이 "0으로 끄지 말 것"이라 경고만 하고
    있었다 — 코드가 막아야 한다.
    """
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: bad})  # type: ignore[arg-type]


def test_redis_socket_timeouts_accept_positive():
    s = Settings(_env_file=None, REDIS_SOCKET_TIMEOUT=0.5, REDIS_SOCKET_CONNECT_TIMEOUT=3)  # type: ignore[call-arg]
    assert s.REDIS_SOCKET_TIMEOUT == 0.5
    assert s.REDIS_SOCKET_CONNECT_TIMEOUT == 3
