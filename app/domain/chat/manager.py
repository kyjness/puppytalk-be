# 워커 로컬 WebSocket 세션. 유저당 다중 소켓(탭·기기). 분산 전달은 Redis → 본 모듈 send.

import asyncio
import logging
from uuid import UUID

from starlette.websockets import WebSocket, WebSocketDisconnect

log = logging.getLogger(__name__)

# DM 분산 브로드캐스트 채널 — 알림 puppytalk:channel:notif:sse 와 네임스페이스 분리.
# 단일 채널 + envelope(수신자 UUID) 규약, 구독·디스패치는 app.infra.pubsub 공용 리스너.
CHAT_DM_FANOUT_CHANNEL = "puppytalk:channel:chat:dm"

# 수신 버퍼가 꽉 찬(죽어가는) 소켓의 send가 무한 대기하면, 이 매니저를 핸들러로 쓰는
# 공용 pubsub 리스너 루프까지 정지한다 — 인스턴스의 실시간 전달 전체가 한 클라이언트에
# 볼모로 잡히지 않게 상한을 두고, 초과 소켓은 끊는다. 소켓별 전송은 병렬이므로
# 유저당 소켓 수와 무관하게 총 대기도 이 상한이다.
_SEND_TIMEOUT_SEC = 5.0


class ConnectionManager:
    """인스턴스(워커) 단위. `user_id` → 해당 유저에 붙은 모든 WebSocket."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._by_user: dict[UUID, set[WebSocket]] = {}

    async def connect(self, user_id: UUID, ws: WebSocket) -> None:
        async with self._lock:
            self._by_user.setdefault(user_id, set()).add(ws)

    async def disconnect(self, user_id: UUID, ws: WebSocket) -> None:
        async with self._lock:
            bucket = self._by_user.get(user_id)
            if not bucket:
                return
            bucket.discard(ws)
            if not bucket:
                del self._by_user[user_id]

    async def send_personal_message(self, user_id: UUID, message: str) -> None:
        async with self._lock:
            sockets = list(self._by_user.get(user_id, ()))
        if not sockets:
            return
        # 소켓별 직렬이면 정체 소켓 N개 = 타임아웃 N배가 pubsub 리스너를 붙잡는다 — 병렬로.
        await asyncio.gather(*(self._send_one(user_id, ws, message) for ws in sockets))

    async def _send_one(self, user_id: UUID, ws: WebSocket, message: str) -> None:
        try:
            await asyncio.wait_for(ws.send_text(message), timeout=_SEND_TIMEOUT_SEC)
        # TimeoutError ⊂ OSError — 타임아웃 소켓도 아래에서 실제로 닫는다.
        except (WebSocketDisconnect, RuntimeError, OSError) as e:
            log.debug("chat ws send skip disconnect user=%s: %s", user_id, e)
            await self._drop(user_id, ws)
        except Exception:
            log.exception("chat ws send error user=%s", user_id)
            await self._drop(user_id, ws)

    async def _drop(self, user_id: UUID, ws: WebSocket) -> None:
        """등록 해제 + 연결 종료. 등록만 지우면 클라이언트는 살아 있는 줄 아는 소켓으로
        계속 보내면서 수신만 조용히 잃는다(재연결 로직도 안 뜬다) — 반드시 닫아서
        클라이언트 측 재연결을 유도한다. 닫기 자체도 정체될 수 있어 짧게 자른다."""
        await self.disconnect(user_id, ws)
        try:
            await asyncio.wait_for(ws.close(code=1011), timeout=1.0)
        except Exception as e:
            log.debug("chat ws close skip user=%s: %s", user_id, e)


chat_connection_manager = ConnectionManager()
