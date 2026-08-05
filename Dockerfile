# syntax=docker/dockerfile:1
# PuppyTalk API 이미지. 멀티스테이지: builder에서 uv로 잠금 의존성만 설치 → runtime은 얇게.
FROM python:3.12-slim AS builder

# uv 바이너리(핀 버전)만 가져와 재현 가능한 설치.
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# 의존성 계층 캐시: 잠금 파일만 먼저 복사해 sync. --no-install-project로 앱 자신은 설치하지 않아
# (소스로 실행 → migrations/alembic.ini 접근), 서드파티 deps만 .venv에 넣는다.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 비루트 실행 유저.
RUN groupadd -r app && useradd -r -g app app

COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations ./migrations
# 운영 CLI(app/scripts/)도 여기 함께 들어온다 — 배포 후 컨테이너에서
# `python -m app.scripts.seed_demo`·`grant_admin`을 실행한다.
COPY app ./app

USER app

EXPOSE 8000

# slim 이미지엔 curl이 없으므로 stdlib로 liveness probe.
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/livez').status == 200 else 1)"]

# compose/ECS가 override 가능(예: alembic upgrade head && gunicorn ...).
CMD ["gunicorn", "app.main:app", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:8000"]
