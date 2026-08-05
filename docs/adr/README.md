# 아키텍처 의사결정 기록 (ADR)

PuppyTalk 백엔드의 주요 설계 결정을 기록한다. 각 ADR은
*"어떤 문제를 어떤 전제 위에서, 왜 이렇게 풀었고, 무엇을 포기했는가"*를 남긴다.

모든 ADR은 [`../00-operating-envelope-and-scope.md`](../00-operating-envelope-and-scope.md)의
**운영 봉투**를 공유한다 — 복잡도는 그 봉투 안에서만 정당화된다.
횡단 결정의 종합은 [`../01-architecture.md`](../01-architecture.md) 참조.

## 목록

| # | 제목 | 성격 | 상태 |
|---|------|------|------|
| [0001](0001-identifier-strategy.md) | 식별자 전략 — UUIDv7·Base62·ULID | 횡단 | 채택됨 |
| [0002](0002-cursor-pagination.md) | Cursor 페이지네이션 — keyset · `total` 제거 | 횡단 | 채택됨 |
| [0003](0003-distributed-rate-limit.md) | 분산 Rate Limit — Redis Lua + smart fail-open | 횡단 | 채택됨 |
| [0004](0004-cache-strategy.md) | 캐시 전략 — 읽기 폭주 경로 · fail-open | 횡단 | 채택됨 |
| [0005](0005-resilience-no-circuit-breaker.md) | 복원력 — fail-open 표준 & CB 미채택 | 횡단 | 채택됨 |
| [0006](0006-observability.md) | 관측성 — 구조화 로그 + 얇은 메트릭 | 횡단 | 채택됨 |
| [0007](0007-view-count-buffering.md) | 조회수 집계 — Redis 버퍼링 + 비동기 Flush | 도메인(posts) | 채택됨 |
| [0008](0008-idempotency-keys.md) | POST 멱등성 — Idempotency-Key + 결과 캐시 | 도메인(posts) | 채택됨 |
| [0009](0009-realtime-delivery.md) | 실시간 전달 — WebSocket·SSE × Redis Pub/Sub | 도메인(chat·notifications) | 채택됨 |
| [0010](0010-storage-backend-strategy.md) | 스토리지 백엔드 — S3 API 단일 경로 + dev MinIO 패리티 | 도메인(media)·Ops | 채택됨 |
| [0011](0011-representative-dog-view-relationship.md) | 대표견 — 전용 뷰 관계 + 부분 유니크 인덱스 | 도메인(dogs·posts·comments) | 채택됨 |
| [0012](0012-admin-report-feed-pagination.md) | 관리자 신고 피드 — DB-side UNION ALL + offset 유지 | 도메인(admin) | 채택됨 |
| [0013](0013-product-behavior-decisions.md) | 제품 동작 결정 — 단일 세션·WS 토큰·차단 시맨틱 | 제품 동작 | 채택됨 |
| [0014](0014-redis-protocol-boundary.md) | Redis 경계 타입 — isinstance 혈통 검사 → RedisLike Protocol | 횡단 | 채택됨 |
| [0015](0015-index-migration-concurrently.md) | 인덱스 마이그레이션 — 라이브 테이블은 CONCURRENTLY | 횡단·Ops | 채택됨 |
| [0016](0016-comment-list-offset-pagination.md) | 댓글 목록 — 인기순 복원을 위한 offset + `total` | 도메인(comments) | 채택됨 |

> 0006의 얇은 메트릭(`/metrics` RED)·헬스 분리(`/livez`·`/readyz`)는 Transition(Ops)에서 구현됐다
> — readiness는 DB=hard·Redis=soft(fail-open)로 구체화(0006 구현 노트).
> 0009는 chat·notifications 재건 단계에서 기구현된 실시간 설계를 소급 근거화했다.
> 0012는 [0002](0002-cursor-pagination.md)의 cursor 표준에 대한 *의도적 예외*(admin 저트래픽·변동 정렬·total 필요)를 근거화한다.

## 형식
각 ADR: **맥락(문제) → 결정 → 트레이드오프 → 고려한 대안 → 일부러 하지 않은 것.**
마지막 "안 한 것"이 *정당화된 복잡도*의 핵심 전시물이다.
