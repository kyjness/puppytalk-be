# 인스턴스 로컬 SSE 팬아웃: 유저별 bounded 큐. Redis 구독은 공용 리스너 1개가 대행한다.

import asyncio
import logging
from uuid import UUID

from app.core.config import settings

log = logging.getLogger(__name__)

# chat(puppytalk:channel:chat:dm)과 네임스페이스만 다른 동일 envelope 규약.
NOTIF_SSE_FANOUT_CHANNEL = "puppytalk:channel:notif:sse"

# 느린 클라이언트가 큐를 다 채우면 신규 이벤트는 버린다(아래 deliver 참조).
_QUEUE_MAX_SIZE = 100


class SseFanoutManager:
    """인스턴스(워커) 단위. `user_id` → 해당 유저의 열린 SSE 스트림 큐들(탭·기기별)."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._by_user: dict[UUID, set[asyncio.Queue[str]]] = {}

    async def register(self, user_id: UUID) -> asyncio.Queue[str] | None:
        """유저당 상한을 넘으면 None — 큐는 bounded지만 연결 수가 무제한이면
        유저 한 명이 인스턴스 로컬 상태를 무한히 늘릴 수 있다(WS와 같은 상한 공유)."""
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_MAX_SIZE)
        async with self._lock:
            bucket = self._by_user.setdefault(user_id, set())
            if len(bucket) >= settings.REALTIME_MAX_CONNECTIONS_PER_USER:
                if not bucket:
                    del self._by_user[user_id]
                log.info("SSE 연결 상한 초과 user=%s open=%s", user_id, len(bucket))
                return None
            bucket.add(queue)
        return queue

    async def unregister(self, user_id: UUID, queue: asyncio.Queue[str]) -> None:
        async with self._lock:
            bucket = self._by_user.get(user_id)
            if not bucket:
                return
            bucket.discard(queue)
            if not bucket:
                del self._by_user[user_id]

    async def deliver(self, user_id: UUID, payload: str) -> None:
        async with self._lock:
            queues = list(self._by_user.get(user_id, ()))
        for queue in queues:
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # 밀린 스트림에 백프레셔를 걸지 않는다 — 알림은 GET /notifications로
                # 재동기화 가능하므로 드롭이 전체 팬아웃 지연보다 낫다.
                log.warning("SSE 큐 가득참, 이벤트 드롭 user=%s", user_id)


notification_sse_manager = SseFanoutManager()
