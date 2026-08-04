# WS Raw JSON → Pydantic 검증. TypeAdapter 단일 인스턴스로 스키마 재사용.

from pydantic import TypeAdapter, ValidationError

from app.domain.chat.schema import ChatMessageSend

_send_adapter: TypeAdapter[ChatMessageSend] = TypeAdapter(ChatMessageSend)


def parse_incoming_message(raw_json: str | bytes) -> ChatMessageSend:
    """JSON 문자열/바이트를 ChatMessageSend로 검증.

    실패 시 ValidationError 발생 → WebSocket 핸들러에서 잡아 ChatWsErrorPayload로 응답하고
    연결은 유지하거나 정책에 따라 종료. 로깅 시 exc_info=False로 스팸 완화 가능.
    """
    return _send_adapter.validate_json(raw_json)


def validation_error_detail(e: ValidationError) -> str:
    """첫 검증 오류를 'loc: msg' 한 줄로 요약 — 에러 프레임 message용(전송은 핸들러 몫)."""
    first = e.errors()[0] if e.errors() else {}
    loc = ".".join(str(x) for x in first.get("loc", ()))
    msg = first.get("msg", "validation_error")
    detail = f"{loc}: {msg}" if loc else str(msg)
    return detail[:500]
