# ADR 0003 — 분산 Rate Limit: Redis Lua Fixed-Window + Smart Fail-open

- **상태**: 채택됨 (Accepted)
- **관련 코드**: `app/core/middleware/rate_limit.py`(`RateLimitMiddleware`,
  `check_fixed_window`, `count_rejection`), `app/api/v1/chat/ws.py`(WS 유저 단위 한도),
  `app/domain/media/router.py`(인증 presign 유저 단위 한도)

## 맥락 (Context)

Rate limit이 필요하다: 로그인 brute-force, 회원가입 이미지 업로드 남용, 전역 남용 방어.
[운영 봉투](../00-operating-envelope-and-scope.md)상 **인스턴스가 3~10대**라, 프로세스 로컬 카운터는
인스턴스마다 따로 세어 실제 한도의 3~10배를 허용해버린다 — 분산 카운터가 필수다.
동시에 Redis가 죽었다고 로그인이 막히면 안 된다(99.9% 가용성).

## 결정 (Decision)

**Redis Lua fixed-window + 스마트 fail-open**을 순수 ASGI 미들웨어로 둔다.

1. **원자 카운트** — Lua로 `INCR` → (첫 요청 시)`EXPIRE` → `TTL`을 한 번에. 네트워크 왕복 1회,
   경합 없는 원자 증가.
2. **Fixed-window** — IP 기준 고정 창, 경로 클래스별 한도(로그인/업로드/전역 — signup
   업로드는 presign·confirm 2경로가 카운터 하나를 공유, 결정 5항).
3. **스마트 fail-open** — Redis 장애 시: **중요 경로(로그인·회원가입 업로드)만** 인메모리 fixed-window로
   폴백(OOM 방지 eviction 포함), 나머지 경로는 **통과**(가용성 우선).
4. **WS DM = 네 번째 한도 클래스**(2차 감사 #32) — WS는 HTTP 미들웨어를 타지 않으므로
   (`scope["type"] != "http"` 통과) 수신 루프에서 **유저 단위**(`chat:ws:{user_id}`) 한도를
   직접 검사한다. 정책은 미들웨어와 같은 단일 진입점 `check_fixed_window`를 공유하되,
   남용 방어 경로라 fail-open이 아니라 **메모리 폴백**(로그인·업로드와 동급). 추가 방어:
   거부 후 retry_after 동안 Redis 왕복 없이 로컬 즉시 거부(스팸의 공유 Redis 부하 증폭
   차단), 연속 거부 누계 30회면 4002 종료(레이트리밋 전용 코드 — 1008은 인증 실패
   전용이라 뭉치면 클라이언트가 스로틀을 재로그인 사유로 오인한다. 코드 계약은
   [ADR 0009](0009-realtime-delivery.md) 참조). 거부 계측은 억제 창 포함 전부
   `count_rejection`으로 `RATE_LIMIT_REJECTIONS{limit="chat"}`에 잡힌다.
5. **미디어 presign 한도 재편**(2차 감사 #24·#31, direct 업로드 제거와 함께) —
   - **비인증(signup)**: 한도 대상 경로를 `signup/presign`·`signup/confirm`으로 이전.
     두 단계가 `signup_upload:{ip}` **카운터 하나를 공유**하고 업로드 1건 = 2카운트라
     기본값을 10→20으로 보정. *안 한 선택*: 단계별 분리 카운터(`signup_presign:` /
     `signup_confirm:`) — 의미론은 더 정밀하지만(presign 전용 예산 고정) 한도 클래스·설정
     키가 2배로 늘고, 1일 pending lifecycle 전제에서 20 presign/h의 잔존 상한(~200MB/h/IP)은
     수용 범위라 공유 카운터의 단순함을 택했다. 실패 재시도가 예산을 더 소모하는 비용은
     명시적 트레이드오프로 수용.
   - **인증 presign**: 글로벌(IP 100/분)만으로는 pending/ 대량 적재를 못 막고, 가입이 열려
     있어 비인증 한도를 일회용 계정으로 우회할 수 있다 — WS DM과 동형으로 라우트에서
     **유저 단위**(`media_presign:{user_id}`, 기본 100/시간) `check_fixed_window` 검사
     (다섯 번째 한도 클래스). confirm은 유효한 1회성 pending 키가 선행돼야 하므로 비용
     원점인 presign만 조인다. 초과는 `TooManyRequestsException`(429, 미들웨어와 동일한
     `retry_after_seconds` data 규격).

## 트레이드오프 (Consequences)

**얻은 것**
- 멀티 인스턴스에서 **일관된 전역 한도** — 인스턴스 수와 무관.
- Lua 원자성으로 카운트 경합·초과 방지.
- Redis 장애에도 로그인 보호는 인메모리로 유지, 서비스 전체는 계속 동작.

**치른 비용**
- **Fixed-window 경계 버스트** — 창 경계에서 순간 최대 2배 허용 가능(봉투상 허용 오차).
- 인메모리 폴백은 인스턴스 로컬이라 장애 중엔 분산 정확도가 떨어짐(중요 경로 한정, 단기).

## 고려한 대안 (Alternatives)

| 대안 | 기각 사유 |
|------|-----------|
| 프로세스 로컬 카운터만 | 멀티 인스턴스에서 한도가 인스턴스 배수로 새어나감 |
| Sliding window log | 요청마다 타임스탬프 집합 저장 → 메모리·복잡도 증가, 요구 대비 과함 |
| Token bucket | 버킷 상태 관리가 더 복잡, fixed-window로 요구 충족 |
| 전 경로 fail-open 없이 fail-closed | Redis 장애가 곧 서비스 장애 → 99.9% 목표와 충돌 |

## 일부러 하지 않은 것 (Non-goals)

- **Sliding window / token bucket**: 경계 버스트 정밀 제어는 이 서비스 요구가 아니다. fixed-window로 충분.
- **전 경로 인메모리 폴백**: 중요 경로만 보호하면 된다. 전역까지 폴백하면 인스턴스별 부정확만 늘 뿐.
