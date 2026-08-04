# PuppyTalk Backend

반려견 커뮤니티 서비스의 백엔드. **FastAPI(Full-Async)** 기반 REST API에 **WebSocket(DM)**·
**SSE(알림)** 를 더한 서버입니다.

이 저장소의 초점은 기능의 개수가 아니라 **"현실적 트래픽을 가정한 운영 등급 백엔드 설계"** 입니다.
초당 수백~수천 조회의 핫스팟, 멀티 인스턴스(3~10대), 무중단 배포를 **의도적으로 전제**하고,
그 전제로 정당화되는 복잡도만 남겼습니다. **정당화되지 않는 과잉은 의식적으로 걷어냈고**, 그 판단
근거를 전부 [ADR](docs/adr/)로 남겼습니다. → *"쓸 데와 안 쓸 데를 구분했다"* 가 핵심입니다.

**관련 링크**

- 프론트엔드: [PuppyTalk Frontend](https://github.com/kyjness/puppytalk-fe)
- 인프라·배포: [PuppyTalk Infra](https://github.com/kyjness/puppytalk-infra) (Terraform·ECS)

---

## 설계 논지 — 정당화된 복잡도

모든 설계 결정은 아래 **운영 봉투(Operating Envelope)** 위에서만 정당화됩니다. 봉투 밖은 과잉으로
판정해 채택하지 않았습니다. 전문은 [`docs/00-operating-envelope-and-scope.md`](docs/00-operating-envelope-and-scope.md).

| 축 | 전제 | 설계 함의 |
|----|------|----------|
| 핫스팟 | 인기글 1건에 **초당 수백~수천 조회** | 조회 경로 최적화 1순위 → 버퍼링·캐시 |
| 쓰기 | 신규 write는 초당 수십 | 강정합성 write는 정직하게 트랜잭션으로 |
| 서버 | 멀티 인스턴스 **3~10대** | 상태는 인스턴스 밖(Redis/DB), 로컬 상태 금지 |
| 배포 | 무중단 롤링 / 블루-그린 | 마이그레이션 하위호환, graceful shutdown, 헬스 분리 |
| 가용성 | **99.9%** | 외부 I/O는 fallback 우선(가용성 > 순간 정합성) |

**상한(넘으면 과잉으로 판정 — 일부러 하지 않은 것):** 멀티리전 · 금융권 강정합성 · 99.99%+ ·
초당 수만 건 *신규 write* · exactly-once 배송 · **AI 기능**(이번 범위에서 완전히 배제).

### 핵심 설계 결정 (ADR)

각 ADR은 *맥락 → 결정 → 트레이드오프 → 고려한 대안 → **일부러 하지 않은 것*** 순으로 남겼습니다.
마지막 "안 한 것"이 정당화된 복잡도의 핵심 전시물입니다.

| # | 결정 | 무엇을 안 했나 |
|---|------|----------------|
| [0001](docs/adr/0001-identifier-strategy.md) | 식별자 3분할 — 내부 PK `UUIDv7` / 공개 ID `Base62` / `ULID`(jti·request_id) | 순차 정수 PK 노출(열거·IDOR 표면) |
| [0002](docs/adr/0002-cursor-pagination.md) | 목록 = **keyset 커서**, `total` 제거 | offset 페이지네이션(deep-offset 스캔 비용) |
| [0003](docs/adr/0003-distributed-rate-limit.md) | 분산 Rate Limit — Redis **Lua fixed-window** + smart fail-open | 인스턴스 로컬 카운터(멀티 인스턴스에서 무의미) |
| [0004](docs/adr/0004-cache-strategy.md) | 캐시는 **읽기 폭주 경로만**, fail-open | 전방위 캐시(무효화 복잡도 대비 이득 없음) |
| [0005](docs/adr/0005-resilience-no-circuit-breaker.md) | 복원력 표준 = **fail-open** | Circuit Breaker(이 규모엔 상태 관리 부담이 과잉) |
| [0006](docs/adr/0006-observability.md) | 구조화 로그 + 얇은 **RED 메트릭** + 헬스 분리 | 분산 트레이싱 백엔드(request_id 상관으로 충분) |
| [0007](docs/adr/0007-view-count-buffering.md) | 조회수 = Redis **버퍼링 + 비동기 flush + 분산락(CAS)** | 매 조회 DB write(핫스팟 폭발) |
| [0008](docs/adr/0008-idempotency-keys.md) | POST 멱등성 — `X-Idempotency-Key` + 결과 캐시 | 중복 제출 방치 / 클라이언트 책임 전가 |
| [0009](docs/adr/0009-realtime-delivery.md) | 실시간 = WebSocket·SSE × **Redis Pub/Sub** fan-out, fail-open | 인스턴스 고정(sticky) 커넥션, 브로커 상시 의존 |
| [0010](docs/adr/0010-storage-backend-strategy.md) | 스토리지 = **S3 API 단일 경로** + dev/CI는 MinIO 패리티 | 로컬 디스크 백엔드(코드 분기·prod 불일치) |
| [0011](docs/adr/0011-representative-dog-view-relationship.md) | 대표견 = 전용 **뷰 관계** + 부분 유니크 인덱스 | 컬렉션 필터 로드(부분 컬렉션 트랩) |
| [0012](docs/adr/0012-admin-report-feed-pagination.md) | 관리자 신고 피드 = DB-side **UNION ALL** + offset·total 유지 | 커서 강제(0002의 *의도적 예외* — 저트래픽·변동 정렬·total 필요) |

> 횡단 관심사 종합은 [`docs/01-architecture.md`](docs/01-architecture.md), 리팩토링 진행·완료
> 이력은 [`docs/ROADMAP.md`](docs/ROADMAP.md), 버그·최적화 백로그와 각 항목 근거는
> [`docs/backlog.md`](docs/backlog.md)에 있습니다.

---

## 기술 스택

| 구분 | 기술 |
|------|------|
| 언어 / 패키지 | Python 3.11+ · uv |
| 프레임워크 | FastAPI · Starlette · Pydantic v2 |
| 서버 | Uvicorn (dev) · Gunicorn + Uvicorn worker (prod) |
| DB / ORM | PostgreSQL · psycopg3(async) · SQLAlchemy 2.x · Alembic |
| 캐시·메시징 | Redis (asyncio) |
| 비동기 작업(선택) | Celery — 알림 dispatch 등 큐 오프로딩 (`CELERY_ENABLED`) |
| 스토리지 | S3 단일 경로 — 실서비스 AWS S3 / dev·CI는 MinIO (boto3, `run_in_threadpool`) |
| 인증 | JWT(PyJWT) · bcrypt(+ pepper) |
| 실시간 | WebSocket(DM) · SSE(알림) × Redis Pub/Sub |
| 관측성 | Prometheus 메트릭 · 구조화 JSON 로그 · `/livez`·`/readyz` |
| 컨테이너·CI | Docker(멀티스테이지) · GitHub Actions · GHCR |

---

## 아키텍처 다이어그램

### 시스템 토폴로지 (운영 봉투 반영)

앱 인스턴스는 **stateless**로 3~10대 수평 확장하고, **모든 상태는 인스턴스 밖**(PostgreSQL·Redis·S3)에 둔다.
실시간(WS·SSE)은 Redis Pub/Sub fan-out으로 인스턴스 경계를 넘어 전달된다.

```mermaid
flowchart TB
    FE["클라이언트<br/>Web / Mobile"]
    ALB["ALB · 무중단 배포<br/>(infra repo · Terraform)"]

    subgraph ECS["ECS — 앱 인스턴스 3~10대 (stateless · gunicorn+uvicorn)"]
        A1["FastAPI 인스턴스<br/>미들웨어 → router → service → repository"]
    end

    W["Celery Worker<br/>알림 dispatch · 정리 배치"]

    subgraph STATE["인스턴스 밖 공유 상태"]
        PGW[("PostgreSQL<br/>Writer")]
        PGR[("PostgreSQL<br/>Reader")]
        REDIS[("Redis<br/>캐시 · RTR · RateLimit<br/>조회수 버퍼 · Pub/Sub")]
        S3["S3 / MinIO<br/>이미지"]
    end

    OBS["관측성<br/>/metrics(Prometheus)<br/>구조화 로그 → CloudWatch"]

    FE -->|"HTTPS /v1/*"| ALB
    ALB -->|"헬스 /livez · /readyz"| ECS
    ALB --> ECS
    ECS -->|CUD| PGW
    ECS -->|조회| PGR
    ECS <-->|"캐시·락·Pub/Sub"| REDIS
    ECS -->|"presigned · put/get"| S3
    REDIS <-->|broker| W
    W --> PGW
    ECS -. scrape · logs .-> OBS
```

### 요청 레이어링 · 횡단 관심사

```mermaid
flowchart LR
    REQ["요청"] --> MW["미들웨어 체인<br/>RequestId · ProxyHeaders · RateLimit<br/>GZip · AccessLog · Metrics · SecurityHeaders"]
    MW --> RT["Router<br/>(api/v1)"]
    RT --> SV["Service<br/>도메인 로직 · 트랜잭션 경계"]
    SV --> RP["Repository<br/>쿼리"]
    RP --> MD["Model<br/>SQLAlchemy 2.x"]
    MD --> DB[("PostgreSQL")]
    SV <-->|"캐시·락·pub/sub · fail-open"| RD[("Redis")]
    SV --> RESP["ApiResponse<br/>camelCase + requestId"]
```

### 조회수 write-behind 버퍼링 (ADR 0007 — 대표 차별점)

초당 수백~수천 조회를 매번 DB write하지 않고, Redis에 버퍼링 후 주기적으로 한 번에 반영한다.

```mermaid
sequenceDiagram
    participant C as Client
    participant App as App 인스턴스
    participant R as Redis
    participant DB as PostgreSQL
    C->>App: POST /v1/posts/{id}/view
    App->>R: SET view:{id}:{viewer} NX EX  (중복 조회 차단)
    alt 신규 조회
        App->>R: HINCRBY views:buffer {id} +1
    end
    App-->>C: 200 (DB write 없음)
    Note over App,DB: 주기 flush — 인스턴스 1대만 (분산락 CAS)
    App->>R: RENAME buffer → drain  (원자 스왑)
    App->>DB: UPDATE view_count += delta
    App->>R: 락 해제 (값 비교 CAS)
```

---

## 아키텍처 · 폴더 구조

`app/` 단일 패키지 아래에 공용 레이어(`api`·`common`·`core`·`db`·`infra`)와 기능 도메인(`domain/`)이
계층화돼 있습니다. 도메인은 **router → service → \[repository →] model → schema** 로 요청이 흐릅니다.
도메인 import는 항상 `app.domain.<영역>` 경로만 사용합니다.

```
app/
├── main.py            # FastAPI 앱, lifespan(설정 검증·조회수 flush·채팅 Redis 구독), 미들웨어·라우터
│                      # 헬스/관측성 엔드포인트: /v1/health · /livez · /readyz · /metrics
├── api/
│   ├── v1/            # /v1 prefix — 도메인 라우터 include + chat REST·WS
│   └── dependencies/  # 인증, DB 세션(get_master_db/get_slave_db), 권한, 쿼리 파싱
├── common/            # ApiResponse·ApiCode·Enum·validators·exceptions·로깅
├── core/
│   ├── config.py      # 설정 + 환경별 프로덕션 가드
│   ├── metrics.py     # 도메인 메트릭(rate-limit 429·캐시 hit/miss·조회수 flush)
│   └── middleware/    # RequestId · ProxyHeaders · RateLimit · GZip · AccessLog · Metrics · SecurityHeaders
├── db/                # SQLAlchemy Base·엔진·세션 (PG_UUID 등 공용 타입)
├── domain/            # admin · auth · chat · comments · dogs · likes · media
│                      # · notifications · posts · reports · users
├── infra/             # Redis(refresh·rate limit·채팅·캐시), storage(S3/MinIO)
└── worker/            # Celery 태스크(선택)

docs/                  # 00(봉투)·01(아키텍처)·adr/·ROADMAP·backlog
migrations/            # Alembic env.py + versions/
tests/                 # unit/(DB 불필요) · integration/(PostgreSQL + pg_trgm)
Dockerfile             # uv 멀티스테이지 · 비루트 · Gunicorn+Uvicorn
.github/workflows/ci.yml
```

---

## 기능 맵

| 기능 | 대표 엔드포인트 | 구현 위치 | 핵심 포인트 |
|------|----------------|-----------|-------------|
| 인증 | `/v1/auth/*` | `domain/auth` | JWT Access/Refresh. Refresh는 **HttpOnly 쿠키 + Redis RTR**, 동시 refresh는 **Lua CAS**로 1건만 성공. 로그아웃은 Access `jti` 블랙리스트. bcrypt는 스레드 오프로딩 + pepper ([0003](docs/adr/0003-distributed-rate-limit.md)) |
| 게시글 | `/v1/posts/*` | `domain/posts` | **커서** 무한 스크롤([0002](docs/adr/0002-cursor-pagination.md)), `q` 검색은 검증 후 **pg_trgm GIN** ILIKE(와일드카드 이스케이프), 해시태그 연동, 생성은 **멱등**([0008](docs/adr/0008-idempotency-keys.md)) |
| 조회수 | `POST /v1/posts/{id}/view` | `domain/posts` + Redis | `SET NX EX` 중복 방지 → Redis 버퍼 누적 → 백그라운드 **flush(분산락 CAS)** ([0007](docs/adr/0007-view-count-buffering.md)) |
| 인기 게시글 | `GET /v1/posts/trending` | `domain/posts` | time-decay 랭킹 + 3단 fallback. **차단 무관 랭킹 풀을 캐시**하고 차단은 요청별 오버레이(사용자별 캐시 폭발 회피) ([0004](docs/adr/0004-cache-strategy.md)) |
| 인기 해시태그 | `GET /v1/posts/trending-hashtags` | `domain/posts` | 빈도 집계 + Redis `TypeAdapter` 캐시(TTL·락), fail-open DB 폴백. 캐시 로직은 `infra/cache.py` 공용 헬퍼 ([0004](docs/adr/0004-cache-strategy.md)) |
| 댓글 | `/v1/comments/*` | `domain/comments` | 루트 **keyset** + 대댓글 배치 로드로 트리 조립(인메모리 슬라이스·하드리밋 제거) |
| 좋아요 | `/v1/likes/*` | `domain/likes` | `ON CONFLICT ... RETURNING` 멱등, `like_count` 동기화, `post_is_visible` 경량 EXISTS |
| 유저 | `/v1/users/*` | `domain/users` | 프로필·비밀번호·탈퇴·차단 목록/토글 |
| 강아지 | `/v1/dogs/*` | `domain/dogs` | 대표견은 전용 뷰 관계 + 부분 유니크 인덱스로 1마리 불변식 보장 ([0011](docs/adr/0011-representative-dog-view-relationship.md)) |
| 채팅(DM) | REST `/v1/chat/*` · WS `/v1/ws/chat` | `domain/chat` | WebSocket + **Redis Pub/Sub fan-out**(멀티 인스턴스), 짧은 트랜잭션으로 커넥션 풀 보호 ([0009](docs/adr/0009-realtime-delivery.md)) |
| 알림 | SSE `/v1/notifications/stream` | `domain/notifications` | 커밋 후 로컬 큐 직접 전달 + Redis envelope 발행 → **SSE** 스트림. Redis 미구성 시에도 스트림 유지(같은 인스턴스 이벤트 수신, fail-open), DB 기록 유지 |
| 이미지 업로드 | `/v1/media/*` | `domain/media` + `infra/storage` | **presigned 3단 단일 경로**(presign → S3 직접 업로드 → confirm, 서버가 파일 본문을 받지 않음). 가입 전은 **1회성 Upload Token**, 고아 이미지 sweeper ([0010](docs/adr/0010-storage-backend-strategy.md)) |
| 신고·모더레이션 | `/v1/reports/*` · `/v1/admin/*` | `domain/reports`·`domain/admin` | 신고는 항상 Insert 누적(감사), 임계 초과 자동 블라인드. 관리자 신고 피드는 DB-side **UNION ALL** ([0012](docs/adr/0012-admin-report-feed-pagination.md)) |

**횡단 규약**: 응답은 `{ code, message, data, requestId }` camelCase 통일 · 요청마다 `request_id`(ULID)
발급·`X-Request-ID` 헤더·로그 상관 · CUD는 서비스에서 `async with db.begin():` 단일 트랜잭션 ·
읽기/쓰기 세션 분리(`get_master_db`/`get_slave_db`) · Rate Limit·캐시·Pub/Sub 등 외부 I/O는 fail-open.

---

## 관측성 · 운영

- **헬스 분리** — `/livez`(의존성 무관 liveness, 실패=재시작) · `/readyz`(readiness — **DB=hard→503**,
  **Redis=soft→report만**). 기존 ALB 경로 `/v1/health`는 하위호환 유지 ([0006](docs/adr/0006-observability.md)).
- **메트릭** — `/metrics`(Prometheus). HTTP RED(`http_requests_total`·`http_request_duration_seconds`·
  in-flight, 라벨 `path`는 라우트 템플릿으로 카디널리티 제한) + 도메인 메트릭(rate-limit 429·캐시
  hit/miss·조회수 flush).
- **로그** — prod=구조화 JSON(stdout), dev=console. `request_id` 상관. 수집(stdout→CloudWatch)은 ECS 몫.
- **컨테이너** — 멀티스테이지 `Dockerfile`, `uv sync --frozen --no-dev`, 비루트 유저, `HEALTHCHECK`=`/livez`.
- **CI/CD** (`.github/workflows/ci.yml`) — quality(lint·format·type·vulture) · test(`postgres:15`+`minio`로
  unit+integration) · security(`pip-audit`) · docker(통과 시 build, `main` push면 **GHCR** push)를 병렬 잡으로.
  실제 배포 대상(ECS)·인프라(ALB·RDS·ElastiCache·CloudWatch)는 인프라 레포(Terraform)에서 관리합니다.

---

## 로컬 실행

전체 스택(DB·Redis·MinIO + API + 프론트)을 한 번에 띄우는 `dev.sh`가 기본 경로입니다.
**의존 서비스만 컨테이너**로 띄우고 **앱은 호스트에서 reload**로 돌립니다 — 코드 수정이
재빌드 없이 즉시 반영됩니다.

```bash
git clone https://github.com/kyjness/puppytalk-be && cd puppytalk-be
./dev.sh                  # 인프라 compose → alembic → uvicorn --reload → 형제 폴더 프론트 pnpm dev
```

- 뜨는 곳: API `localhost:8000`(Swagger `/v1/docs`) · 프론트 `localhost:5173` · MinIO 콘솔 `localhost:9001`.
- 첫 실행이면 `.env`(없으면 `.env.example` 복사)와 `.venv`(`uv sync`)를 자동으로 준비합니다.
- `Ctrl+C`는 호스트 프로세스(백엔드·프론트)만 멈춥니다. 컨테이너 종료는 `./dev.sh --down`.
- 옵션: `--backend-only` · `--docker`(백엔드까지 컨테이너로 — 아래 참고) · `FE_DIR=`(프론트 경로).
  poe 별칭 `stack`·`stack-backend`·`stack-docker`·`stack-down`도 동일.

세부 경로(전체 컨테이너 실행·수동 실행)·검사/테스트·API 문서는 아래에 접어 두었습니다.

<details>
<summary><b>다른 실행 방법 — 전체 컨테이너 · 수동 실행</b></summary>

**A. 전체 컨테이너 (`docker compose`)** — `docker-compose.yml`은 DB·Redis·MinIO에 더해 **백엔드 서비스도
정의**합니다. 개발 자격이 compose에 인라인 주입되어 **`.env`도 `.venv`도 없이 클론 직후 바로** 뜹니다.
배포와 동일한 이미지(`alembic upgrade head && gunicorn`)라 환경 검증·데모용으로 씁니다. 대신 소스 마운트가
없어 코드 수정마다 재빌드가 필요하므로, 개발 루프에는 위의 기본 경로를 쓰세요.

```bash
docker compose up --build -d          # 전체 기동 (backend는 db/redis healthcheck 후 alembic → gunicorn)
curl http://localhost:8000/v1/health  # {"code":"OK",...} 확인
docker compose logs -f backend        # 로그
docker compose down -v                # 중지 + 볼륨 초기화
```

`./dev.sh --docker`는 여기에 프론트 `pnpm dev`까지 붙여줍니다. MinIO 콘솔 `localhost:9001`
(minioadmin/minioadmin), 미디어 버킷(`puppytalk`)은 `minio-init`가 자동 생성·공개. 실 배포 이미지도
같은 `Dockerfile`을 ECS/PaaS가 사용합니다.

**B. 수동 실행** — `dev.sh`가 하는 일을 손으로. Python 3.11+ 필요.

```bash
docker compose up -d db redis minio minio-init   # 1. 의존 서비스만
uv sync --extra dev            # 2. 의존성(런타임 + dev: pytest·ruff·pyright·vulture·poe)

cp .env.example .env           # 3. 환경 변수 — DB_*, JWT_SECRET_KEY, REDIS_URL 등
#  - ENVIRONMENT=development(기본) | production — production 계열은 강한 JWT_SECRET_KEY(32자+) 필수
#  - 스토리지: dev는 MinIO(S3_ENDPOINT_URL 지정), prod는 실제 S3 자격 필수

uv run poe migrate             # 4. DB 스키마 = alembic upgrade head (새 마이그레이션 생기면 재실행)
uv run poe run                 # 5. 서버 http://localhost:8000 (문서 /v1/docs · 헬스 /v1/health)

uv run poe celery-worker       # (선택) Celery — CELERY_ENABLED=true 일 때만
uv run poe celery-beat
```

프로덕션 기동 시 `validate_settings_for_environment()`가 `JWT_SECRET_KEY`(placeholder 금지·32자+)와
S3 자격 등을 검사하며, 위반 시 프로세스를 중단합니다.

</details>

<details>
<summary><b>검사 · 테스트 (CI와 동일한 게이트)</b></summary>

`ci.yml`이 돌리는 것과 **같은 poe 태스크**를 로컬에서 그대로 실행합니다(push 전 선검증). 태스크 정의는
`pyproject.toml`의 `[tool.poe.tasks]`가 단일 출처입니다.

```bash
uv run poe quality   # lint(--fix) + format
uv run poe check     # lint-check + format-check + typecheck + vulture-check + audit (= CI quality/security 잡)

# 테스트 — 통합 테스트는 puppytalk_test DB 필요
export TEST_DB_URL="postgresql+psycopg://postgres:PASSWORD@localhost:5432/puppytalk_test"
uv run pytest tests/unit           # DB 불필요(검색 검증·트렌드 쿼리·조회수 버퍼·도메인 메트릭 등)
uv run pytest tests/integration    # PostgreSQL + pg_trgm (스키마는 conftest가 세션 시작 시 생성)
```

통합 스위트는 세션 스코프 스키마를 공유하며(테스트 간 롤백 없음), Redis 저장소가 필요한 RTR·멱등성
테스트는 Redis 미연결 시 자동 skip합니다. 컨테이너로 CI의 DB를 재현하려면:

```bash
docker run -d --name pg -e POSTGRES_PASSWORD=PASSWORD -e POSTGRES_DB=puppytalk_test -p 5432:5432 postgres:15
```

</details>

<details>
<summary><b>API 문서 · 관리자 계정</b></summary>

| 환경 | Swagger UI | ReDoc | OpenAPI JSON |
|------|-----------|-------|--------------|
| 로컬 | `/v1/docs` | `/v1/redoc` | `/v1/openapi.json` |
| 프로덕션 | `https://api.puppytalk.shop/v1/docs` | `/v1/redoc` | `/v1/openapi.json` |

응답·스펙 필드는 프론트 OpenAPI codegen 계약을 위해 **camelCase**로 노출됩니다.

관리자 API(`/v1/admin/*`)는 `role='ADMIN'` 유저만 사용할 수 있습니다:

```sql
UPDATE users SET role = 'ADMIN' WHERE email = 'your-admin@example.com';
```

</details>

---

## 배포

공개 데모는 **인스턴스 한 대에서 compose로** 돕니다 — Caddy(TLS 종단) · API(gunicorn 2) ·
Celery 워커 · PostgreSQL · Redis. 관리형 서비스(RDS·ElastiCache·ALB)를 쓰지 않는 대신 HA·자동
백업·무중단 배포를 포기한 **쇼케이스 등급** 배포이며, 그 판단은
[ADR 0016](docs/adr/0016-demo-deployment-topology.md)에 있습니다. AWS 리소스는
[인프라 레포의 `demo/`](https://github.com/kyjness/puppytalk-infra/tree/main/demo) terraform root가
만듭니다.

| 파일 | 역할 |
|------|------|
| [`compose.prod.yml`](compose.prod.yml) | 배포 스택. GHCR 이미지를 pull하며 외부로는 Caddy(80·443)만 연다 |
| [`Caddyfile`](Caddyfile) | `api.<도메인>` 자동 HTTPS · SSE 버퍼링 해제 · WebSocket 업그레이드 |
| [`.env.prod.example`](.env.prod.example) | 프로덕션 가드가 요구하는 값 전체(실제 값은 서버에만 둔다) |
| [`scripts/seed_demo.py`](scripts/seed_demo.py) | 데모 데이터 시드 — 빈 사이트에서는 목록·트렌딩·DM·알림을 보여줄 수 없다 |
| [`scripts/backup_db.sh`](scripts/backup_db.sh) | 일일 `pg_dump` → S3(14일 보관). 컨테이너 볼륨이 유일한 사본이라 필요하다 |
| [`.github/workflows/cd.yml`](.github/workflows/cd.yml) | CI 성공 후 SSH로 `pull && up -d` + 헬스 체크 |

```bash
# 서버에서 (배포 파일 4개가 /opt/puppytalk 에 있다고 가정)
docker compose --env-file .env.prod -f compose.prod.yml up -d
docker compose --env-file .env.prod -f compose.prod.yml exec backend python -m scripts.seed_demo
```

전체 적용 절차(도메인 위임·IAM 키 발급·GitHub Secrets)는
[인프라 README 9장](https://github.com/kyjness/puppytalk-infra#9-라이브-데모-트랙-demo--적용-중)에 있습니다.

---

## 문서

| 문서 | 설명 |
|------|------|
| [00 · 운영 봉투와 범위](docs/00-operating-envelope-and-scope.md) | 모든 설계·복잡도 판정의 단일 근거(전제·과제·재건 순서) |
| [01 · 아키텍처](docs/01-architecture.md) | 횡단 관심사 결정(식별자·API·트랜잭션·캐시·페이지네이션·관측성·인덱스) |
| [ADR](docs/adr/) | 핵심 설계 결정 16건 — 각 결정의 트레이드오프와 *안 한 것* |
| [ROADMAP](docs/ROADMAP.md) | RUP-lite 리팩토링 진행·완료 이력(도메인 단위 + 커밋) |
| [backlog](docs/backlog.md) | 버그·최적화 백로그와 각 항목의 근거·수정 방향 |
