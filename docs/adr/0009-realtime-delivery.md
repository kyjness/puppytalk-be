# ADR 0009 — 실시간 전달: WebSocket(chat)·SSE(notifications) × Redis Pub/Sub

- **상태**: 채택됨 (Accepted)
- **관련 코드**:
  `app/api/v1/chat/ws.py`(WebSocket `/ws/chat`),
  `app/domain/chat/manager.py`(워커-로컬 `ConnectionManager`),
  `app/domain/chat/service.py`(`send_dm_from_ws`·`_fanout_dm`),
  `app/domain/notifications/router.py`(SSE `/notifications/stream`),
  `app/domain/notifications/service.py`(`publish_after_commit`·`sse_subscribe`),
  `app/domain/notifications/stream.py`(워커-로컬 `SseFanoutManager`),
  `app/infra/pubsub.py`(envelope publish·공용 구독 리스너),
  `app/worker/jobs/notification_delivery.py`(Celery SNS 배송 잡),
  `app/infra/redis.py`·`app/main.py`(풀 커넥션·lifespan 리스너 배선)

## 맥락 (Context)

[운영 봉투](../00-operating-envelope-and-scope.md)는 **멀티 인스턴스 · 무중단 재배포**를 전제한다.
그런데 실시간 연결(채팅 소켓·알림 스트림)은 본질적으로 **특정 워커 프로세스에 붙는다** — LB 뒤 N개
인스턴스 중 하나의 메모리에 소켓이 산다. 이벤트의 발생지(상대 유저의 액션을 처리한 요청, 혹은 Celery
워커)는 **다른 워커**일 수 있다. 단일 프로세스라면 인메모리 연결 맵으로 끝나지만, 멀티 인스턴스에서는
"발생지 워커 → 수신자 소켓을 가진 워커"로 이벤트를 넘길 **프로세스 간 fanout**이 필요하다.

또한 채팅과 알림은 **전송 방향이 다르다**. 채팅은 클라이언트가 메시지를 **보내고 받는**(양방향)
반면, 알림은 서버가 이벤트를 **밀어주기만**(단방향, 클라는 수신만) 한다. 한 전송 방식으로 억지로
통일하면 한쪽이 과하거나 부족해진다.

## 결정 (Decision)

**채팅은 WebSocket, 알림은 SSE로 전달하고, 두 경로 모두 프로세스 간 fanout을 Redis Pub/Sub로
해결한다.** 실시간은 **at-most-once 최선 전달**이며, 지속적 진실은 항상 DB다.

1. **전송 선택 = 방향성에 맞춤**
   - **채팅 → WebSocket**(`/ws/chat`). 클라가 소켓으로 메시지를 송신(`send_dm_from_ws`)하므로
     양방향 전이중이 필요. 인증은 `?token=` Access JWT(jti 블랙리스트 확인).
   - **종료 코드 계약**: `1008`=인증·권한 실패(`ws_close_code`의 4xx), `4001`=유저당 동시 연결
     상한 초과, `4002`=메시지 레이트리밋 초과(429 예외 포함), `1013`=정체·사망 소켓 정리
     (매니저 send 타임아웃 — 재접속 유도, 서버 오류 아님), `1011`=내부 오류(5xx). 용량 사유를
     1008에서 갈라낸 이유는 **복구 동작이 다르기 때문**이다 — 1008은 재인증, 4001은 재연결
     포기, 4002는 백오프 재연결. 한 코드로 뭉치면 클라이언트가 구분할 수 없어, 탭을 여러 개
     열거나 빠르게 도배한 것뿐인데도 로그인 세션을 폐기하는 오작동이 난다(실제로 그랬다).
   - **알림 → SSE**(`/notifications/stream`). 서버→클라 단방향이라 SSE로 충분 — HTTP 위에서 동작,
     `EventSource` 자동 재연결, 프록시 친화적, 업그레이드 핸드셰이크 불필요. 25초 `: ping` 하트비트로
     유휴 연결 유지.

2. **멀티 인스턴스 fanout = Redis Pub/Sub, 채팅·알림 공용 패턴**
   - **단일 채널 + envelope `{origin, target_user_ids, payload}`** — 채팅
     `puppytalk:channel:chat:dm`, 알림 `puppytalk:channel:notif:sse`(네임스페이스만 분리).
     같은 wire를 받는 수신자들(DM의 peer·sender)은 **목록으로 envelope 1건**에 담는다 —
     건별 발행은 발행 RTT와 전 인스턴스 파싱을 수신자 수만큼 증폭한다. 롤링 배포 창
     호환은 양방향으로 닫는다(at-most-once 세맨틱 안): 파서는 구포맷 스칼라
     `target_user_id`를 수용하고(구→신), 발행자는 첫 수신자를 스칼라 키로 병기한다
     (신→구 — 구 리스너에는 첫 수신자만 전달되는 부분 열화. 전 인스턴스 교체 후
     다음 릴리스에서 병기 제거).
   - **로컬 우선 전달**: 발행자는 같은 인스턴스 수신자에게 **로컬 매니저로 먼저 직접
     전달한 뒤** publish한다. publish 성공은 "Redis에 넘김"일 뿐 로컬 도달을 보장하지
     않기 때문(소비는 별도 리스너 연결 몫 — 리스너 재연결 창에서는 성공한 publish도
     로컬에 도달하지 않는다). 리스너는 envelope `origin`(프로세스별 지연 생성 ID,
     preload-then-fork에서도 워커별 고유)이 자기 것이면 건너뛰어 중복을 막는다.
     부수 효과로 로컬 수신 지연에서 Redis 왕복이 빠진다.
   - **인스턴스당 전용 Redis 연결 1개**가 두 채널을 함께 구독(`run_user_fanout_listener`,
     lifespan 기동)하고, envelope의 `target_user_ids`를 **로컬 매니저**로 넘긴다 — 채팅은
     `ConnectionManager`(WS 소켓), 알림은 `SseFanoutManager`(SSE 스트림별 bounded 큐).
   - SSE 스트림(`sse_subscribe`)은 Redis를 만지지 않고 **로컬 큐 대기**만 한다. 초기 설계의
     "연결마다 유저별 채널 구독"은 SSE 동시 연결 수만큼 공유 풀(128) pubsub을 점유해, 풀 한도
     근접 시 rate limit·인증 캐시·조회수 버퍼가 연쇄 fail-open되는 결함이라 폐기했다(2차 감사 #23).
   - **요청 I/O용 풀 커넥션(`app.state.redis`)과 Pub/Sub 전용 소켓을 분리**한다 —
     구독 루프는 오래 블록되므로 풀을 점유하면 안 된다.

3. **fail-open 복원력** ([ADR 0005](0005-resilience-no-circuit-breaker.md) 정합)
   - Redis가 없거나(`None`) publish/subscribe가 실패해도 **DB·인앱 데이터·로컬 전달은
     유지**된다(로컬 우선 전달이 Redis에 의존하지 않으므로). publish 실패 시 유실되는
     것은 **다른 인스턴스 수신자의 실시간 이벤트뿐**이며 `GET /notifications`·재접속으로
     동기화한다. 실시간 전달 실패가 **쓰기 트랜잭션을 절대 되돌리지 않는다**.
     알림 SSE 엔드포인트도 Redis 부재 시 503이 아니라 스트림을 유지한다.
   - **리스너 백오프 재연결**: 접속 실패·수신 계층 예외 1회로 리스너가 죽으면 크로스
     인스턴스 전달이 프로세스 재시작까지 전멸한다 — 0.5s→30s 지수 백오프로 재연결하고,
     리셋은 구독 성공이 아니라 **연결 5s 생존 후**에만 한다(구독만 통과하고 곧 죽는
     플래핑이 0.5s 고정 재연결 루프를 만드는 것 방지). 기동도 부팅 핑 성공(`app.state.redis`)에
     게이트하지 않는다 — 배포 중 Redis 순단이 리스너를 영구 비활성화하면 안 된다.
   - **매니저 send 타임아웃(5s)**: 공용 리스너가 로컬 전달을 직접 await하므로, 수신 버퍼가
     찬 죽은 소켓 하나가 인스턴스의 실시간 전달 전체를 볼모로 잡을 수 있다(head-of-line).
     타임아웃 초과 소켓은 등록 해제 후 **실제로 닫는다**(닫아야 클라이언트 재연결 로직이
     뜬다). 트레이드오프: 5s 이상 정체된 "느리지만 살아 있는" 클라이언트도 끊긴다 — 수용
     (재접속·GET 재동기 경로 존재). 큐 기반 소켓별 전달(정체 격리 + 순서 보장)은 현
     운영 봉투에서 미정당 복잡도로 보류(backlog #37).

4. **오프라인 배송(SNS) 오프로드 = Celery**
   - 실시간 인앱(pub/sub)은 인라인으로 두고, **재시도·백오프가 필요한 외부 I/O인 SNS publish만**
     알림 생성 시 `deliver_notification_sns`(high_priority 큐)로 오프로드한다 — "쓸 데(외부 배송)와
     안 쓸 데(인라인으로 충분한 실시간 발행)"의 구분.
   - 멱등키 `celery:notif:delivered:{key}`는 **publish 성공 후에만 마킹**한다 — 선마킹하면 실패
     재시도가 멱등 skip으로 유실된다. 경쟁 중복 publish(at-least-once)는 SNS 구독자가 흡수.
   - `CELERY_ENABLED=false`·브로커 장애 시 인라인 fire-and-forget으로 폴백(fail-open).
     페이로드는 태스크 인자가 아니라 워커가 DB 행에서 재구성한다(재시도 시점에도 진실은 DB).
   - 초기의 사용자 트리거 재전달 API(`POST /notifications/{id}/dispatch`)는 실제 UX 흐름이 없는
     합성 경로라 제거했다(2차 감사 #22).

## 트레이드오프 (Consequences)

**얻은 것**
- 멀티 인스턴스·무중단에서 실시간 전달 성립 — 소켓이 어느 워커에 붙든 이벤트가 도달.
- 전송 방식이 방향성과 일치 — 채팅은 양방향, 알림은 경량 단방향.
- 실시간 장애가 데이터 정합을 깨지 않음(DB가 진실, 클라 재동기 경로 존재).

**치른 비용**
- **at-most-once**: Pub/Sub는 fire-and-forget이라 수신자가 오프라인이거나 워커가 publish 순간
  재시작 중이면 그 실시간 이벤트는 유실된다 — DB가 진실이고 클라가 GET으로 재동기하므로 수용.
- **단일 채널의 워커별 필터링**: 모든 워커가 모든 envelope를 수신해 `target_user_ids`로
  거른다(워커 수 × 메시지 수). 운영 봉투 내에서는 수용하되, 초고fanout 시 채널 샤딩이 탈출구다.
- **느린 SSE 클라이언트의 이벤트 드롭**: 로컬 큐(100)가 차면 신규 이벤트를 버린다 —
  백프레셔로 전체 팬아웃을 지연시키는 것보다 낫고, 클라는 목록 API로 재동기한다.
- **전송 이원화**: WebSocket·SSE 두 경로를 유지·테스트해야 한다.

## 고려한 대안 (Alternatives)

| 대안 | 기각 사유 |
|------|-----------|
| Sticky session(LB가 유저를 워커에 고정, Redis 없이 인메모리) | 무중단 재배포·스케일아웃에서 깨짐. 워커 사망 시 전달 소실, 다기기 크로스-워커 불가 |
| 알림도 WebSocket | 단방향 이벤트에 양방향 전이중은 과함. SSE 자동 재연결·HTTP 친화성 이점 상실 |
| 채팅도 SSE | 채팅은 클라 송신이 필요 — SSE는 서버→클라 단방향이라 별도 POST 채널을 덧대야 함 |
| 외부 브로커(Kafka·RabbitMQ)로 fanout | 지속성·순서 보장은 at-most-once 실시간에 불필요. 운영 봉투에서 브로커 운영비 정당화 안 됨(지속성은 DB+Celery가 담당) |
| 클라이언트 폴링 | 지연·부하. 채팅/알림 실시간성은 제품 요구 |

## 일부러 하지 않은 것 (Non-goals)

- **전달 보장(ack·replay·오프라인 큐)**: 실시간은 at-most-once로 두고 지속성은 DB에 위임한다.
  재접속 시 GET 목록으로 재동기 — 실시간 계층에 durable queue를 얹지 않는다.
- **전송 계층 통일**: 전송 시맨틱(양방향 vs 단방향)이 달라 WS·SSE는 각자 유지한다 — 공용화는
  fanout 계층(단일 채널+envelope+공용 리스너)까지만("쓸 데·안 쓸 데 구분").
- **sse-starlette 도입**: `data:`/`: ping` 프레이밍을 직접 다뤄 의존성 1개를 줄인다.
- **Redis Cluster 슬롯 최적화**: 두 경로 모두 단일 채널이라 크로스-슬롯 이슈가 없다.
  클러스터 도입은 별도 결정으로 미룬다.
