# 미들웨어(라우팅 전 실행)가 참조하는 실경로 상수.
#
# rate limit 미들웨어는 라우트 매칭 전에 동작해 경로를 문자열로 판별한다 — 라우터 소유
# 경로의 사본이 흩어져 있으면 라우트 개명 시 전용 한도가 조용히 글로벌로 강등되고,
# 미들웨어 테스트도 드리프트를 못 잡는다. 상수를 한 곳에 두고 드리프트 가드 테스트
# (tests/unit/test_path_constants.py — app.routes와 대조)로 고정한다.

from app.core.config import settings

_prefix = settings.API_PREFIX.rstrip("/")

LOGIN_PATH = f"{_prefix}/auth/login"
SIGNUP_PRESIGN_PATH = f"{_prefix}/media/images/signup/presign"
SIGNUP_CONFIRM_PATH = f"{_prefix}/media/images/signup/confirm"

# 프로브·계측 경로(앱 루트, 인프라 전용) — rate limit·메트릭 미들웨어가 공유하는 스킵 목록.
INFRA_PROBE_PATHS = frozenset({"/livez", "/readyz", "/metrics"})
