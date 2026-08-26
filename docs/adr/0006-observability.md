# ADR 0006 — 관측성: 구조화 로그 + 얇은 메트릭 & 트레이싱 백엔드 미채택

- **상태**: 채택됨 (Accepted) · 메트릭·헬스 분리는 Transition(Ops)에서 구현(아래 구현 노트)
- **관련 코드**: `app/common/logging_config.py`, `app/core/middleware/request_id.py`,
  `app/core/middleware/access_log.py`, `app/core/middleware/metrics.py`(`/metrics`),
  `app/main.py`(`/livez`·`/readyz`)

## 맥락 (Context)

[운영 봉투](../00-operating-envelope-and-scope.md)상 인스턴스가 3~10대라, 장애·성능 문제를
**어느 인스턴스에서든 상관 추적**할 수 있어야 한다. ECS→CloudWatch 환경에서 로그를 필드로 쿼리하고
(`request_id`·`path`·`status`·`duration_ms`), 무중단 배포 시 준비된 인스턴스에만 트래픽을 보내야 한다.
현재 `request_id` 전파는 이미 훌륭하나, 로그가 plain text라 필드 쿼리가 어렵고 메트릭은 전무하다.

## 결정 (Decision)

**구조화 로그 + 얇은 메트릭**을 두고, **분산 트레이싱 백엔드는 채택하지 않는다.**

1. **JSON 구조화 로그(프로덕션)** — `request_id·method·path·status·duration_ms·client_ip`를 필드로.
   개발 환경은 사람이 읽는 콘솔 포맷으로 분기. `request_id` 전파(state·contextvars·헤더)는 유지.
2. **얇은 메트릭** — `prometheus-client`로 `/metrics` 노출. 핵심만: 요청 수·지연 히스토그램·에러율
   + 도메인 지표(조회수 flush 건수·rate limit 429·캐시 hit/miss). ECS에서 스크레이프.
3. **헬스 분리** — `/health`를 liveness(shallow) + readiness(deep: DB·Redis ping)로 나눠 롤링/블루-그린
   배포에서 준비된 인스턴스에만 트래픽.
4. **트레이싱 백엔드 미채택** — 아래 Non-goals 참조.

## 트레이드오프 (Consequences)

**얻은 것**
- 로그를 필드로 쿼리 → 장애 원인 추적 시간 단축.
- 요청률·지연·에러율 + 도메인 지표로 봉투 가정(조회 폭주 등)을 실측으로 검증 가능.
- 헬스 분리로 무중단 배포 안전성 확보.

**치른 비용**
- JSON 로그는 사람이 눈으로 읽기엔 덜 편함(개발 콘솔 포맷 분기로 완화).
- 메트릭 카디널리티(라벨 남용 시 폭증) 관리 필요 — 라벨을 저카디널리티로 제한.

## 고려한 대안 (Alternatives)

| 대안 | 기각 사유 |
|------|-----------|
| plain text 로그 유지 | 필드 쿼리 불가(grep만) → 멀티 인스턴스 상관 추적 곤란 |
| OpenTelemetry 풀 스택(Collector·트레이싱 백엔드) | 3~10 인스턴스 + request_id 로그 상관으로 충분 → 운영 부담 대비 과잉 |
| APM SaaS(Datadog 등) | 비용·범위 밖. 포트폴리오 목적엔 self-hosted 얇은 메트릭으로 충분 |

## 일부러 하지 않은 것 (Non-goals)

- **분산 트레이싱 백엔드(OTel Collector·Jaeger)**: `request_id` 기반 로그 상관으로 이 규모의 추적은
  충분하다. 트레이싱 인프라는 봉투 상한 밖 복잡도 — **의식적으로 배제.**
- **커스텀 대시보드 코드**: 메트릭 노출까지만. 시각화는 운영 도구(Grafana 등) 몫으로 남긴다.

## 구현 노트 (Transition/Ops)

이 ADR의 메트릭·헬스 분리를 Ops 단계에서 구현하며, 결정 문구를 아래처럼 구체화했다.

- **프로브는 Host 검사를 건너뛴다.** `/livez`·`/readyz`·`/metrics`를 부르는 것은 사용자가 아니라
  인프라다 — Docker `HEALTHCHECK`는 컨테이너 안에서 `localhost`로, ALB·kubelet은 파드 IP로
  때린다. 그 값들은 `TRUSTED_HOSTS`(공개 도메인 목록)에 들어갈 이유가 없고, 넣으면 Host 검사
  범위만 넓어진다. 제외하지 않으면 **앱이 정상인데도 프로브가 400을 받아 영구 unhealthy**가
  된다 — 첫 데모 배포에서 실제로 그랬다(`Host: api.puppytalk.shop` 200 / `Host: localhost` 400).
  rate limit·관측 미들웨어가 이미 쓰던 `INFRA_PROBE_PATHS` 상수를 공유한다.
- **헬스 분리 — DB=hard·Redis=soft.** `/livez`는 의존성 체크 없이 프로세스 생존만(실패=재시작),
  `/readyz`는 DB 실패 시에만 503(라우팅 제외)한다. 결정 문구는 readiness를 "DB·Redis ping"으로
  적었으나, **Redis는 fail-open**([ADR 0003](0003-distributed-rate-limit.md)·
  [0005](0005-resilience-no-circuit-breaker.md))이라 DB만 살아있으면 인스턴스는 (열화된 채로)
  서빙 가능하다. Redis로 readiness를 gate하면 얻는 것 없이 용량만 깎이므로 **Redis는 ping해서
  payload에 report만 하고 gate하지 않는다**(hard=DB, soft=Redis). 기존 ALB 경로 `/v1/health`는
  하위호환으로 유지 — 인프라의 ALB→`/readyz`·liveness→`/livez` 전환은 be-repo 밖(문서화만).
- **얇은 메트릭 — RED 우선.** `prometheus-client` default registry로 `http_requests_total`
  (method·path·status)·`http_request_duration_seconds`(히스토그램)·`http_requests_in_progress`
  (in-flight)를 `access_log`와 동형 미들웨어로 계측한다. **라벨 `path`는 라우트 템플릿**
  (`/v1/posts/{post_id}`)을 써 저카디널리티를 지키고, `/metrics`·`/livez`·`/readyz`는 기록 제외.
- **도메인 지표(구현됨).** 결정 2의 도메인 지표를 `app/core/metrics.py`에 두고 계측한다:
  `rate_limit_rejections_total{limit}`(429를 login·signup_upload·global별로),
  `cache_events_total{cache,result}`(트렌딩 해시태그 hit/miss),
  `view_buffer_flushed_views_total`(조회수 flush로 DB 반영된 view 합). 전송 계층 RED 지표
  (`middleware/metrics.py`)와 분리하되 같은 default registry로 `/metrics`에 함께 노출 — 운영 봉투
  가정(조회 폭주·RL 압력·캐시 효율)을 실측한다.
