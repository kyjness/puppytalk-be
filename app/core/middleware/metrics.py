# Prometheus RED 메트릭 계기(요청 수·지연·in-flight). 계측 호출은 observability_middleware가 담당.
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.requests import Request

# 라벨 path는 raw URL이 아니라 라우트 템플릿(/v1/posts/{post_id})을 쓴다 — 경로 파라미터가
# 값마다 새 시계열을 만드는 카디널리티 폭증을 막는다. probe·/metrics 자신은 기록에서 제외.
_UNMATCHED = "__unmatched__"

REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "HTTP 요청 총계",
    ["method", "path", "status"],
)
REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP 요청 처리 시간(초)",
    ["method", "path"],
)
# 라우트 템플릿은 call_next 이후에야 알 수 있어 in-flight 시점엔 미상이므로, 오해를 부르는
# path 라벨 대신 method로만 계측한다(전체 in-flight 포화도).
IN_PROGRESS = Gauge(
    "http_requests_in_progress",
    "처리 중인 HTTP 요청 수(in-flight)",
    ["method"],
)


def route_template_label(request: Request) -> str:
    """매칭된 라우트의 경로 템플릿(call_next 이후 scope["route"]가 채워진다). 미매칭은 고정 라벨."""
    route = request.scope.get("route")
    return getattr(route, "path", None) or _UNMATCHED


def render_metrics() -> tuple[bytes, str]:
    """/metrics 노출용 (본문, content-type)."""
    return generate_latest(), CONTENT_TYPE_LATEST
