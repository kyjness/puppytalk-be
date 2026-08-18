# ADR 0008 — POST 멱등성: Idempotency-Key + 결과 캐시

- **상태**: 채택됨 (Accepted) · **적용 범위 축소** — 미디어 direct 업로드 제거([0010](0010-storage-backend-strategy.md))로
  현재 적용 대상은 `POST /posts` 하나. presigned confirm은 1회성 pending 키(승격 시 원본
  삭제)가 **중복 부작용을 차단**(at-most-once)하므로 이 메커니즘 없이도 중복 생성은 없다.
  단, 결과 캐시가 주던 **성공 응답 재생은 없다** — confirm 성공 후 응답이 유실되면 재시도는
  400으로 실패하고 클라이언트는 presign부터 다시 시작해야 한다(업로드 1건 재시도 비용 수용,
  글 중복 생성 같은 데이터 훼손이 없어 트레이드오프로 허용).
- **관련 코드**: `app/domain/posts/idempotency.py`(`post_create_idempotency_before`/
  `_after_success`/`_after_failure`), `app/domain/posts/router.py`(`POST /posts`),
  `app/infra/lock.py`(in-flight 락 — 랜덤 토큰 + CAS 해제)

## 맥락 (Context)

게시글 생성·이미지 업로드 같은 **생성(POST)** 은 부작용이 있다. [운영 봉투](../00-operating-envelope-and-scope.md)가
가정하는 모바일·불안정 네트워크에서는 클라이언트가 응답을 못 받고 **재시도**하거나, 사용자가 버튼을
연타(double-submit)한다. 그대로 두면 글이 중복 생성되고, 이미지가 여러 장 업로드돼 고아를 만든다.
인스턴스가 3~10대(봉투)이므로 중복 방지는 **인스턴스 간 공유(Redis)** 여야 한다 — 프로세스 메모리로는
무력하다.

## 결정 (Decision)

**"Opt-in `X-Idempotency-Key` + Redis 결과 캐시 + in-flight 락"** 을 생성 계열의 멱등성 표준으로 한다.

1. **Opt-in 헤더** — 클라이언트가 `X-Idempotency-Key`(8~128자)를 보낼 때만 작동. 없으면 일반 처리
   (조회·비생성은 대상 아님). 키가 부실하면(`after_*`) 조용히 통과.
2. **스코프된 fingerprint** — `sha256(namespace scope_parts + key)`. 스코프에 **사용자 id** +
   연산 구분을 넣어 네임스페이스(`post:create`)별로 격리한다. 한 사용자의 키가 다른 사용자와
   충돌하거나 캐시를 열람하지 못한다.
3. **두 Redis 키** — 결과 캐시 `idemp:{ns}:res:{fp}`(성공 응답, TTL)와 in-flight 락
   `idemp:{ns}:lock:{fp}`(`SET NX`, 짧은 TTL).
   락은 **공용 프리미티브**(`app/infra/lock.py`)를 쓴다 — 획득 시 랜덤 토큰을 발급하고
   해제는 값 비교 **CAS**다. 직접 `SET NX` 후 `DEL`로 풀면 락 TTL이 만료된 뒤 뒤늦게 끝난
   요청이 *다음* 요청의 락을 지워, 같은 키로 두 건이 동시에 생성된다
   ([ADR 0007](0007-view-count-buffering.md)이 조회수 flush에서 같은 이유로 단순 delete를
   기각한 것과 같은 판단). 토큰은 before → after 훅으로 `IdempotencyHandle`에 실려 전달되며,
   fail-open 구간에서는 None이라 해제를 시도하지 않는다.
4. **흐름** — *before*: 결과 캐시 히트면 저장된 응답을 그대로 재생(단, `requestId`는 현재 요청 값으로
   갱신) → 없으면 `SET NX`로 락 시도, 이미 점유면 **409**(같은 키가 처리 중). *after_success*: 결과
   저장 + 락 해제. *after_failure*: 락만 해제(재시도 허용).
5. **Fail-open** — Redis 오류 시 멱등성 없이 진행한다([ADR 0005](0005-resilience-no-circuit-breaker.md)
   표준). 가용성 > 중복 방지.

## 트레이드오프 (Consequences)

**얻은 것**
- 안전한 클라이언트 재시도·연타 방어 — DB 유니크 제약 없이도 생성 중복을 막는다.
- 인스턴스 간 정확 — Redis 공유라 3~10대 봉투에서 일관.
- 저렴 — 키 2개·TTL로 끝.

**치른 비용**
- **exactly-once 아님** — 성공 커밋과 결과 저장 사이의 크래시, 또는 fail-open 창에서는 재시도가
  재실행될 수 있다(at-least-once + best-effort dedup).
- 결과 캐시는 TTL 동안만 — 재시도 창(수십 분)만 커버하고 영구 보장은 아니다.
- 409는 거친 신호 — 처리 중 재시도를 "충돌"로 돌려보내 클라이언트가 잠시 후 재시도해야 한다.
- 클라이언트가 **안정적인 키**를 보내야 효과가 있다(계약 문서화 필요).

## 고려한 대안 (Alternatives)

| 대안 | 기각 사유 |
|------|-----------|
| DB 유니크 제약 기반 멱등 | 도메인마다 자연키 설계가 필요하고, 업로드처럼 자연키 없는 연산엔 부적합 |
| 프로세스 메모리 dedup | 멀티 인스턴스(봉투 3~10대)에서 무력 |
| 전 요청 자동 멱등(GET 포함) | 조회까지 비용을 물림 — 대부분 불필요. Opt-in이 맞다 |
| exactly-once(2PC·트랜잭셔널 아웃박스) | 봉투 상한 초과 — 결제·정산이 아니다 |

## 일부러 하지 않은 것 (Non-goals)

- **exactly-once 보장**: at-least-once + 결과 캐시 재생으로 충분. 정확히 한 번은 봉투 상한을 넘는 복잡도.
- **글로벌(사용자 무관) dedup**: 키를 user/ip로 스코프해 격리 — 크로스 사용자 캐시 오염·열람을 차단.
- **영구 멱등 원장(DB 저장)**: TTL 캐시로 현실적 재시도 창만 커버한다. 무기한 보관은 저장·정리 부채.
