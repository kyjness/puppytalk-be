# ADR 0005 — 복원력: Fail-open 표준 & Circuit Breaker 미채택

- **상태**: 채택됨 (Accepted)
- **관련 코드**: `app/infra/redis.py`, `app/core/middleware/rate_limit.py`,
  `app/domain/posts/services/post_service.py`(view buffer), `app/infra/storage.py`

## 맥락 (Context)

외부 I/O 의존(Redis·S3)이 있다. [운영 봉투](../00-operating-envelope-and-scope.md)의 가용성 목표는
**99.9%**이고 상한은 멀티리전·강정합성이 아니다. 즉 외부 의존 하나가 흔들릴 때 **전체 가용성을
지키는 것**이 순간 정합성보다 우선이다. 관건은 "어디까지 방어 장치를 넣는가"다.

## 결정 (Decision)

**Fail-open을 복원력 표준으로 명문화**하고, **Circuit Breaker는 채택하지 않는다.**

1. **Fail-open 표준** — Redis 장애 시: 조회수 dedup/버퍼를 건너뛰고 DB 직접 증가, rate limit은
   통과/인메모리 폴백([ADR 0003](0003-distributed-rate-limit.md)), 캐시는 DB 폴백([ADR 0004](0004-cache-strategy.md)).
   "성능·보호 계층은 없어도 서비스는 돈다."
2. **경계 타임아웃 — fail-open의 전제.** 외부 I/O 클라이언트에는 반드시 **소켓 타임아웃**을 둔다.
   위 1의 fail-open은 전부 `except`로 발동하므로, **유한한 대기 없이는 성립하지 않는다.**
   의존이 *죽은* 것(프로세스 down → connection refused)과 *먹통인* 것(네트워크 분단·보안그룹
   차단·페일오버 → 패킷이 조용히 사라짐)은 다른 실패 모드이고, 타임아웃이 없으면 후자에서
   예외 자체가 발생하지 않아 fail-open 코드가 **한 줄도 실행되지 않는다.**
   Redis는 rate limit 미들웨어·인증 캐시가 매 요청 경유하므로, 이 경우 의존 하나의 이상이
   곧 전체 장애가 된다 — 이 ADR이 막으려는 바로 그 결과다.
   구체값은 `REDIS_SOCKET_TIMEOUT`(1s)·`REDIS_SOCKET_CONNECT_TIMEOUT`(2s)이고, 설정 창구는
   `app/infra/redis.py::redis_connection_kwargs` **하나**다 — 클라이언트 생성부가 셋이라
   (앱 풀·구독 소켓·워커) 각자 쓰면 그중 하나만 옵션이 빠지는 식으로 조용히 갈라진다.
   Celery 브로커·결과 백엔드는 `app/core/celery.py`에서 따로 설정하며 결과는 읽지 않으므로
   `task_ignore_result`로 발행 시 결과 백엔드 구독 자체를 없앤다.
   **소켓 타임아웃이 못 덮는 곳이 하나 있다** — 구독 소켓의 폴 루프. redis-py의
   `get_message(timeout=…)`는 명시 타임아웃이 소켓 타임아웃을 덮어쓰고 `None`을 반환하므로,
   구독 성립 후 먹통이 되면 예외가 영영 안 난다. 그래서 리스너는 **앱 수준 워치독**(유휴 시
   PING, pong 부재 시 끊고 재연결)으로 유한성을 확보한다(`app/infra/pubsub.py`).
   같은 이유로 DB readiness 프로브도 `asyncio.wait_for` 한 번으로는 부족하다 — psycopg가 첫
   취소를 잡아 재대기하므로 이중 취소로 끝낸다(`app/db/connection.py::_bounded`).
   교훈은 하나다: **"타임아웃을 걸었다"와 "실제로 유한하다"는 다르고, 가짜 의존성 테스트는
   그 차이를 못 본다** — 드라이버가 실제로 어떻게 대기·취소하는지를 확인해야 한다.
   S3는 **presigned POST라 업로드가 서버를 경유하지 않고**(`app/infra/storage.py`), 서버가 직접
   호출하는 삭제 경로는 `run_in_threadpool`로 이벤트 루프와 분리돼 있어 별도 경계가 필요 없다.
3. **Circuit Breaker 미채택** — 아래 Non-goals 참조.

## 트레이드오프 (Consequences)

**얻은 것**
- 외부 의존 장애가 **전체 장애로 전파되지 않음** — 99.9% 목표에 직결.
- 상태 기계(CB의 open/half-open) 없이 단순 — 추론·운영이 쉬움.

**치른 비용**
- 장애 중 **보호 계층 약화** — 예: Redis 다운 시 조회수 dedup이 잠깐 사라져 수치가 부풀 수 있음.
  조회수는 비임계 지표라 봉투상 허용.
- CB가 주는 "장애 대상 빠른 차단·자동 회복"은 없음 — 타임아웃+재시도로 대체.
- **먹통 구간의 응답 지연은 0이 아니라 "유한"이다.** 타임아웃은 무한 대기를 없앨 뿐 대기를
  없애지 않는다. 인증 요청 하나가 Redis를 3회 경유하므로(rate limit → jti 블랙리스트 →
  인증 캐시), 각 호출이 타임아웃을 물면 최악값이 수 초까지 늘어난다. 이를 더 줄이려면
  "연속 실패 시 일정 시간 Redis를 건너뛴다"가 필요한데 그것이 곧 circuit breaker이므로,
  **여기서 멈추는 것이 이 ADR의 선택**이다. 봉투(99.9%)에서 드문 장애 구간의 수 초 지연은
  상시 상태 기계의 복잡도보다 싸다. 이 전제가 깨지면(먹통이 잦거나 지연이 SLO를 먹으면)
  CB 미채택 결정을 다시 연다.

## 고려한 대안 (Alternatives)

| 대안 | 기각 사유 |
|------|-----------|
| Fail-closed | 외부 의존 장애 = 서비스 장애 → 99.9% 목표와 정면충돌 |
| Circuit Breaker 도입 | open/half-open 상태·임계 튜닝의 복잡도. 봉투 규모에선 타임아웃+fail-open으로 충분 |
| 재시도 큐/사가 | 조회수·캐시 같은 비임계 경로엔 불필요한 인프라 |

## 일부러 하지 않은 것 (Non-goals)

- **Circuit Breaker**: 이 서비스의 외부 의존은 소수(Redis·S3)이고, 각기 fail-open + 타임아웃으로
  방어된다. CB의 상태 기계는 봉투 상한(멀티리전·초당 수만) 아래에서 정당화되지 않는 복잡도 —
  **의식적으로 배제한다.** ("쓸 데와 안 쓸 데를 구분했다"의 대표 사례.)
- **분산 트랜잭션 / Saga**: 강정합성은 봉투 상한 밖. 비임계 경로는 최종 일관성으로 충분.
