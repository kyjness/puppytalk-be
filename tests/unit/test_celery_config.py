"""Celery 설정이 요청 경로를 Redis에 매달리게 하지 않는지 (ADR 0005·0018).

`.delay()`는 알림 발행 요청 경로에서 불린다. Celery는 `ignore_result`가 아니면 발행 시
**결과 백엔드에 pubsub.subscribe** 한다(`app.send_task` → `backend.on_task_call`) — 그런데
결과 백엔드 클라이언트는 앱 공용 `redis_connection_kwargs()`를 거치지 않는 별도 생성부라,
Celery 기본값(`redis_socket_timeout=120s`, connect 무제한)을 그대로 쓴다. 먹통 Redis에서
댓글·좋아요 응답이 인라인 폴백으로 넘어가기 전에 그만큼 매달린다.

이 앱은 태스크 결과를 **읽는 곳이 없다.** 그러니 구독 자체를 없애는 것이 정답이고, 방어로
백엔드 타임아웃도 공용 설정을 따르게 한다.
"""

from app.core.celery import celery_app
from app.core.config import settings


def test_task_results_are_ignored_so_enqueue_does_not_subscribe():
    assert celery_app.conf.task_ignore_result is True, (
        "결과를 읽는 곳이 없는데 ignore_result가 아니면 매 enqueue가 결과 백엔드를 구독한다"
    )


def test_result_backend_client_carries_socket_timeouts():
    """`ignore_result`가 언젠가 풀리더라도 백엔드 소켓이 무제한으로 남지 않게."""
    assert celery_app.conf.redis_socket_timeout == settings.REDIS_SOCKET_TIMEOUT
    assert celery_app.conf.redis_socket_connect_timeout == settings.REDIS_SOCKET_CONNECT_TIMEOUT
