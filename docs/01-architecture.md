# 01 · 아키텍처 — 횡단 관심사 결정

> **Elaboration 산출물 — 확정.** [`00-operating-envelope-and-scope.md`](00-operating-envelope-and-scope.md)의
> 운영 봉투 위에서 도출한 횡단 관심사 결정. 각 결정은 *현행 → 결정* 으로 남긴다.
> load-bearing 결정은 하단 **ADR 목록**으로 근거화한다. (원칙: "적게, 그러나 핵심은 반드시")

관통 기준: **조회 폭주(초당 수백~수천) · 멀티 인스턴스 3~10대 · 무중단 · 99.9%.**
그 상한(멀티리전·초당 수만 write·99.99%+)을 넘는 복잡도는 *의식적으로 배제*하고 "안 한 선택"으로 기록.

---

## C1. 기반 계약

| 항목 | 현행 | 결정 |
|------|------|------|
| **식별자** | UUIDv7(PK)·Base62(공개)·ULID(추적) + 레거시 ULID 수용 폴백 | 역할 분리 **유지**(시간정렬 PK + 순차 비노출·짧은 공개 ID). **레거시 ULID 폴백 제거** — 공개 ID는 Base62/UUID만 수용 |
| **API 일관성** | `ApiResponse[T]{code,data,message,request_id}`, camelCase | **그대로 확정.** 모든 엔드포인트 `ApiResponse[T]`. (목록은 C2에서 `total` 제거) |
| **설정** | 평범한 class + `os.getenv`, JWT만 검증 | **pydantic-settings 전환** + 프로덕션 가드(`COOKIE_SECURE`·`TRUSTED_HOSTS≠[*]`·`DB_PASSWORD` 비어있지 않음·CORS localhost 없음). Alembic 순환참조 회피(config 독립) 유지 |
| **트랜잭션/UoW** | `get_master_db`/`get_slave_db`(R/W 분리), 서비스에서 `begin()` | **경계=서비스**(라우터 아님), CUD=단일 `begin()`. R/W 분리 **유지** + **쓰기 직후 읽기는 master**(복제 지연 규약) |

## C2. 데이터 접근

| 항목 | 현행 | 결정 |
|------|------|------|
| **페이지네이션** | posts는 UUIDv7 PK **keyset**(모범), admin·댓글은 인메모리(#5·#6) | **keyset(cursor) 표준으로 통일**. 인메모리 제거. 응답 = `{items, has_more, next_cursor}` — **`total` 제거**(#10, COUNT 비용·의미 약함). 댓글 트리는 "루트 keyset + 대댓글 부모별 로드" ⚠️ *아래 개정 참조* |

> **개정 — 페이지네이션 (2건, 후속 ADR로 일부 뒤집힘).**
> 위 "keyset 전면 통일"은 Elaboration 시점의 결정이며, 이후 두 곳에서 **의도적 예외**가 확정됐다.
> 표준 자체(기본은 keyset·`total` 제거)는 유지되고, 예외는 아래 둘뿐이다.
>
> | 대상 | 현재 | 근거 |
> |------|------|------|
> | 관리자 신고 피드 | DB-side `UNION ALL` + **offset·`total` 유지** | [ADR 0012](adr/0012-admin-report-feed-pagination.md) — 저트래픽·변동 정렬·`total` 필요 |
> | **댓글 루트 목록** | **offset + `total`** (기본 정렬 인기순) | [ADR 0016](adr/0016-comment-list-offset-pagination.md) — 정렬 축 `like_count`가 변동값이라 keyset 불성립. 게시글 1건에 국한된 유한 집합이라 deep-offset이 실질 문제가 아님 |
>
> 대댓글(`/{comment_id}/replies`)은 정렬 축이 불변이라 **커서를 유지**한다.
> 즉 위 표의 "루트 keyset + 대댓글 부모별 로드"는 **루트/대댓글이 반대로 뒤집힌 상태**가 현재다.
| **인덱스** | pg_trgm GIN·부분 인덱스·FK 인덱스 (양호) | 원칙: **"조회 패턴이 인덱스를 결정"**, 추가·제거는 Alembic + EXPLAIN 근거. **`UserBlock` 중복 UniqueConstraint 제거**(#13). chat 미읽음(#16)은 쿼리 수정 우선 |
| **낙관적 락** | `version`(User·Post만), ORM 수정 경로 | **현행 유지 · 확장 안 함.** "동시 수정 충돌이 실재하는 애그리거트에만, 남발 금지". 조회수는 Core update라 무관 |

## C3. Redis · 복원력

| 항목 | 현행 | 결정 |
|------|------|------|
| **캐시** | `user:status` 캐시 존재하나 `get_current_user`에 미적용(#7) | 원칙: **"읽기 폭주 경로만 · fail-open · 명시적 무효화"**. 인증 상태 캐시를 **핫 경로에 연결**(#7). 상태 변경 시 DEL + refresh 토큰 revoke(#8). **범용 캐시 추상화는 배제** — 얇은 get-or-set + fail-open 헬퍼만 |
| **Rate Limit** | Redis Lua fixed-window + smart fail-open(중요 경로만 메모리 폴백) | **현행 유지**(봉투상 정당·이미 모범). 여지: 필요 시 `user_id` 버킷(지금 과제 아님) |
| **Fallback** | fail-open 전 구간 일관, Circuit Breaker 미구현 | fail-open을 **표준으로 명문화**("가용성 > 순간 정합성"). **Circuit Breaker 미채택**(봉투 하에서 과잉) — 단 사용자 대면 외부 I/O(S3)엔 **타임아웃 + 명확한 실패 응답** |

## C4. 관측성

| 항목 | 현행 | 결정 |
|------|------|------|
| **추적** | `request_id`(ULID) → state·contextvars·헤더·로그·응답 전파 (훌륭) | **유지** |
| **로그** | plain text / access_log는 key=value | **JSON 구조화 로그로 전환**(프로덕션). 개발은 사람이 읽는 콘솔 포맷(환경별 분기) |
| **메트릭** | 없음 | **`prometheus-client`로 `/metrics` 얇게 도입** — 요청 수·지연·에러율 + 도메인(조회수 flush·429·캐시 hit/miss). **트레이싱 백엔드(OTel/Jaeger)는 미채택** |
| **헬스** | `/health`(shallow) | **liveness(shallow) + readiness(deep: DB·Redis ping) 분리** — 무중단 배포에서 준비된 인스턴스에만 트래픽 |

---

## ADR 목록 (load-bearing만)

작성 시점 = 해당 결정을 실제로 손댈 때(Elaboration 횡단 / Construction 도메인). 남발하지 않는다.

| # | 제목 | 성격 | 시점 |
|---|------|------|------|
| 0001 | 식별자 전략 — UUIDv7(PK)·Base62(공개), 레거시 폐기 | 횡단 | Elaboration |
| 0002 | Cursor 페이지네이션 — keyset · `total` 제거 | 횡단 | Elaboration |
| 0003 | 분산 Rate Limit — Redis Lua fixed-window + smart fail-open | 횡단 | Elaboration |
| 0004 | 캐시 전략 — 읽기 폭주 경로 · fail-open · 명시적 무효화 | 횡단 | Elaboration |
| 0005 | 복원력 — fail-open 표준 & **Circuit Breaker 미채택(non-goal)** | 횡단 | Elaboration |
| 0006 | 관측성 — 구조화 로그 + 얇은 메트릭 & **트레이싱 백엔드 미채택** | 횡단 | Elaboration |
| 0007 | 조회수 집계 — Redis 버퍼링 + 비동기 Flush(분산락·Lua CAS) | 도메인(posts) | Construction |
| 0008 | POST 멱등성 — Idempotency-Key + 결과 캐시 | 도메인(posts) | Construction |
| 0009 | 실시간 전달 — WebSocket·SSE × Redis Pub/Sub | 도메인(chat·notifications) | Construction |
| 0010 | 스토리지 백엔드 — S3 API 단일 경로 + dev MinIO 패리티(local 폐기) | 도메인(media)·Ops | Construction |
| 0011 | 대표견 — 전용 뷰 관계 + 부분 유니크 인덱스 | 도메인(dogs) | Construction |
| 0012 | 관리자 신고 피드 — UNION ALL + offset 유지 (**0002의 예외**) | 도메인(admin) | Construction |
| 0013 | 제품 동작 — 단일 세션 · WS 토큰 전달 · 차단 시맨틱 | 제품 동작 | Construction |
| 0014 | Redis 경계 타입 — `RedisLike` Protocol | 횡단 | Construction |
| 0015 | 인덱스 마이그레이션 — 라이브 테이블은 `CONCURRENTLY` | 횡단·Ops | 3차 감사 |
| 0016 | 댓글 루트 목록 — offset + `total` (**0002의 예외**) | 도메인(comments) | 3차 감사 |
| 0017 | 라이브 데모 배포 — 단일 인스턴스 compose | Ops | Transition |

> ADR 형식: **맥락(문제) → 결정 → 트레이드오프 → 고려한 대안 → 일부러 안 한 것.**
> "안 한 선택"(#0005 CB, #0006 트레이싱)이 오히려 *정당화된 복잡도*의 핵심 전시물.
