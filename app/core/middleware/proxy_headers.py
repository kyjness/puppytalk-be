# Nginx/ALB 뒤에서 실제 클라이언트 IP를 scope['client']에 반영. 신뢰 프록시 IP 대역에서만 X-Forwarded-For 파싱(IP 스푸핑 방어).
import ipaddress

from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import settings


def client_ip_from_scope(scope: Scope, *, default: str = "unknown") -> str:
    """프록시 검증이 끝난 scope['client']의 IP. 이 모듈이 신뢰 경계를 소유하므로
    IP를 읽는 쪽(레이트리밋 키·접근 로그·조회수 dedup)은 전부 여기를 거친다 —
    원시 X-Forwarded-For를 직접 읽으면 임의 위조가 가능하다. default는 호출부의
    기존 센티널을 보존한다(Redis 키에 들어가므로 통일하면 키가 바뀐다)."""
    client = scope.get("client")
    if client and client[0]:
        return client[0]
    return default


def _parse_trusted_networks(
    allowed: list[str],
) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """설정 문자열을 네트워크 객체로 변환(단일 IP는 /32·/128 네트워크로 통일). 무효 항목은 버린다."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in allowed:
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return networks


def _is_trusted_proxy(
    direct_client_ip: str,
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
    *,
    allow_all: bool,
) -> bool:
    if allow_all:
        return True
    try:
        client = ipaddress.ip_address(direct_client_ip)
    except ValueError:
        return False
    return any(client in net for net in networks)


class ProxyHeadersMiddleware:
    """순수 ASGI. X-Forwarded-For 검증 후 scope['client'] 갱신. add_middleware 시 RateLimit보다 바깥에 두어 먼저 실행."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        # 신뢰 프록시 목록은 정적 설정 — 요청마다 ip_network() 파싱을 반복하지 않도록 1회 변환.
        self._trusted_networks = _parse_trusted_networks(settings.TRUSTED_PROXY_IPS)
        self._allow_all = not settings.TRUSTED_PROXY_IPS

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if settings.TRUST_X_FORWARDED_FOR:
            client = scope.get("client") or ("", 0)
            if _is_trusted_proxy(client[0], self._trusted_networks, allow_all=self._allow_all):
                for raw_name, raw_val in scope.get("headers") or []:
                    if raw_name.lower() == b"x-forwarded-for" and raw_val:
                        forwarded = raw_val.decode("utf-8", errors="replace").strip()
                        if forwarded:
                            scope["client"] = (forwarded.split(",")[0].strip(), 0)
                        break
        await self.app(scope, receive, send)
