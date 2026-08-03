#!/usr/bin/env bash
# 로컬 개발 전체 스택 — 인프라(db·redis·minio)만 컨테이너로 띄우고,
# 백엔드·프론트는 호스트에서 reload로 돌린다. 코드 수정이 재빌드 없이 즉시 반영된다.
#
# 사용:
#   ./dev.sh                 # 인프라 + 백엔드(:8000) + 프론트(:5173)
#   ./dev.sh --backend-only  # 인프라 + 백엔드만
#   ./dev.sh --docker        # 백엔드까지 컨테이너로(배포 이미지 그대로) + 프론트. 데모·환경검증용
#   ./dev.sh --down          # 컨테이너 종료
#   FE_DIR=/path/to/fe ./dev.sh   # 프론트 경로 지정(기본: ../puppytalk-fe)
#
# Ctrl+C는 호스트 프로세스(백엔드·프론트)만 멈춘다 — 컨테이너는 유지(재시작이 빠르다).
set -euo pipefail

# 잡 컨트롤 — 백그라운드 잡이 각자 프로세스 그룹을 갖게 해서 종료 시 자식(uvicorn reload
# 워커·vite)까지 그룹째 정리한다. 끄면 pnpm만 죽고 vite가 5173을 물고 남는다.
set -m

BE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FE_DIR="${FE_DIR:-$BE_DIR/../puppytalk-fe}"
HEALTH_URL="${HEALTH_URL:-http://localhost:8000/v1/health}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-90}"
INFRA_SERVICES=(db redis minio minio-init)

compose() { docker compose -f "$BE_DIR/docker-compose.yml" "$@"; }

JOB_PIDS=()
cleanup() {
  trap - EXIT INT TERM
  for pid in "${JOB_PIDS[@]:-}"; do
    [[ -n "$pid" ]] || continue
    kill -- -"$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
  done
}

usage() { sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

MODE=host
case "${1:-}" in
  --down)
    echo "▶ 컨테이너 종료…"
    compose down
    exit 0
    ;;
  -h | --help)
    usage
    exit 0
    ;;
  --docker) MODE=docker ;;
  --backend-only) MODE=backend-only ;;
  "") ;;
  *)
    echo "✗ 알 수 없는 옵션: $1" >&2
    usage >&2
    exit 1
    ;;
esac

command -v docker >/dev/null || {
  echo "✗ docker가 필요합니다."
  exit 1
}

# 컨테이너가 healthcheck를 통과할 때까지 대기. healthcheck 없는 서비스(minio 등)는 건너뛴다.
wait_healthy() {
  local svc="$1" cid status
  cid="$(compose ps -q "$svc")"
  [[ -n "$cid" ]] || return 0
  for ((i = 1; i <= 60; i++)); do
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || echo none)"
    case "$status" in
      healthy | none) return 0 ;;
      unhealthy)
        echo "  ✗ $svc unhealthy — 로그: docker compose logs $svc" >&2
        return 1
        ;;
    esac
    sleep 1
  done
  echo "  ✗ $svc가 60s 내 준비되지 않음 — 로그: docker compose logs $svc" >&2
  return 1
}

wait_api() {
  echo "▶ API health 대기: $HEALTH_URL (최대 ${HEALTH_TIMEOUT}s)"
  for ((i = 1; i <= HEALTH_TIMEOUT; i++)); do
    if curl -sf "$HEALTH_URL" >/dev/null 2>&1; then
      echo "  ✓ API ready"
      return 0
    fi
    sleep 1
  done
  echo "  ✗ API가 ${HEALTH_TIMEOUT}s 내 준비되지 않음" >&2
  return 1
}

start_frontend() {
  if [[ ! -d "$FE_DIR" ]]; then
    echo "⚠ 프론트 디렉터리를 찾지 못함: $FE_DIR"
    echo "  FE_DIR=/path/to/fe 로 지정하거나, 백엔드만 쓰려면 --backend-only 사용."
    return 1
  fi
  command -v pnpm >/dev/null || {
    echo "✗ pnpm이 필요합니다. corepack enable 후 재시도." >&2
    return 1
  }
  [[ -d "$FE_DIR/node_modules" ]] || (cd "$FE_DIR" && pnpm install)
  (cd "$FE_DIR" && pnpm dev) &
  JOB_PIDS+=("$!")
}

# --- A. 전체 컨테이너 경로(데모·환경검증) — 배포와 동일한 이미지로 백엔드까지 띄운다 ---
if [[ "$MODE" == docker ]]; then
  echo "▶ 전체 스택 기동 (docker compose up --build -d)…"
  compose up --build -d
  wait_api
  trap cleanup EXIT INT TERM
  echo "▶ 프론트 개발 서버 — Ctrl+C로 종료(컨테이너는 계속 실행, 종료는 ./dev.sh --down)"
  start_frontend || exit 0
  wait
  exit 0
fi

# --- B. 기본 경로 — 인프라만 컨테이너, 앱은 호스트에서 reload ---
echo "▶ 인프라 기동 (${INFRA_SERVICES[*]})…"
compose up -d "${INFRA_SERVICES[@]}"

# 배포 이미지로 띄운 backend 컨테이너가 남아 있으면 8000 포트가 겹친다.
if [[ -n "$(compose ps -q backend 2>/dev/null)" ]]; then
  echo "▶ 이전 backend 컨테이너 정리 (호스트 실행과 8000 충돌)…"
  compose stop backend >/dev/null
fi

wait_healthy db
wait_healthy redis

if [[ ! -f "$BE_DIR/.env" ]]; then
  echo "▶ .env 생성 (.env.example 복사) — 필요하면 값을 수정하세요."
  cp "$BE_DIR/.env.example" "$BE_DIR/.env"
fi

if [[ ! -x "$BE_DIR/.venv/bin/python" ]]; then
  command -v uv >/dev/null || {
    echo "✗ .venv가 없고 uv도 없습니다. https://docs.astral.sh/uv 설치 후 재시도." >&2
    exit 1
  }
  echo "▶ 의존성 설치 (uv sync --extra dev --frozen)…"
  (cd "$BE_DIR" && uv sync --extra dev --frozen)
fi
PY="$BE_DIR/.venv/bin/python"

# 호스트 실행은 compose와 달리 .env를 읽는다 — 값이 컨테이너 인프라와 어긋나면 여기서 잡는다.
"$PY" - <<'PY' || true
from app.core.config import settings

missing = [
    name
    for name in ("S3_ENDPOINT_URL", "S3_BUCKET_NAME", "S3_PUBLIC_BASE_URL")
    if not getattr(settings, name)
]
if missing:
    print(f"⚠ .env에 {', '.join(missing)}가 비어 있습니다 — 이미지 업로드가 동작하지 않습니다.")
    print("  .env.example의 '스토리지' 블록(MinIO 값)을 .env에 반영하세요.")
PY

echo "▶ DB 마이그레이션 (alembic upgrade head)…"
if ! (cd "$BE_DIR" && "$PY" -m alembic upgrade head); then
  echo "✗ 마이그레이션 실패 — .env의 DB_*가 compose db와 어긋났을 수 있습니다." >&2
  echo "  compose db 기본값: postgres / postgres @ localhost:5432 / puppytalk (.env.example과 동일)" >&2
  echo "  볼륨을 먼저 초기화하려면: ./dev.sh --down && docker compose down -v" >&2
  exit 1
fi

trap cleanup EXIT INT TERM

if curl -sf "$HEALTH_URL" >/dev/null 2>&1; then
  echo "▶ 이미 8000에 응답하는 백엔드가 있습니다 — 새로 띄우지 않습니다."
else
  echo "▶ 백엔드 (uvicorn --reload, http://localhost:8000 · 문서 /v1/docs)"
  # 이 레포는 WSL ext4에 있어 inotify가 동작한다. watchfiles는 WSL 커널을 보면 폴링으로
  # 떨어지는데, 폴링은 .venv까지 훑어 유휴 상태에서도 CPU를 태운다.
  (cd "$BE_DIR" && WATCHFILES_FORCE_POLLING=false \
    "$PY" -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000) &
  JOB_PIDS+=("$!")
fi

if [[ "$MODE" == backend-only ]]; then
  echo "백엔드만 실행 중 — Ctrl+C로 종료(컨테이너 종료는 ./dev.sh --down)"
  wait
  exit 0
fi

echo "▶ 프론트 개발 서버 (pnpm dev, http://localhost:5173)"
echo "  Ctrl+C = 백엔드·프론트 동시 종료. 컨테이너 종료는 ./dev.sh --down"
start_frontend || true
wait
