# ADR 0014 — Redis 경계 타입: isinstance(Redis) → RedisLike Protocol

- **상태**: 채택됨 (Accepted)
- **관련 코드**: `app/infra/redis.py`(`RedisLike`·`get_app_redis`·`close_redis`),
  `app/domain/chat/router.py`(WS jti 조회 — 구 `ws_auth.py`), `tests/unit/fakes.py`

## 맥락 (Context)

앱 lifespan에 붙는 Redis 클라이언트의 접근 가드(`get_app_redis` 등)는
`isinstance(raw, Redis)` — **실클라이언트 혈통 검사**였다. 이 검사 하나가 연쇄를 만들었다:

1. 단위 테스트의 `FakeRedis`가 가드를 통과하려면 실제 `Redis`를 **상속**해야 한다.
2. 상속하면 pyright가 가짜의 간소한 메서드를 업스트림 시그니처와 대조해
   `reportIncompatibleMethodOverride` 8건을 낸다.
3. 이를 가리려고 `typings/redis/asyncio/__init__.pyi` 로컬 스텁으로 **업스트림
   타입 전체를 덮어썼다** — redis-py 6.x는 `py.typed`로 온전한 async 타입을 제공하는데도.

결과: pyright "0 errors"가 부분적으로 가짜였다(스텁이 보이는 한 실클라이언트 타입은
검사 대상이 아님). 스텁에 없는 `from_url` 때문에 프로덕션 코드에 `cast(Any, Redis)`
우회가 2곳(pubsub·notification worker) 생겼고, 스텁은 앱이 쓰지 않는 명령
(sadd/srem/sismember)까지 담은 채 낡아갔다. **테스트 편의가 프로덕션 타입 정보를
가리는 구조** — 꼬리가 몸통을 흔들고 있었다.

## 결정 (Decision)

가드를 **혈통 검사에서 능력 검사로** 바꾼다.

- `app/infra/redis.py`에 `@runtime_checkable` **`RedisLike` Protocol**을 정의한다.
  멤버는 앱이 실제 호출하는 12개 명령만(ping·aclose·get·set·setex·delete·eval·evalsha·
  hget·hgetall·hincrby·publish). 구독 소켓은 공유 풀을 쓰지 않고 전용 연결을 따로
  만들므로(`app/infra/pubsub.py`) `pubsub`은 이 계약에 없다.
- 가드를 `isinstance(x, RedisLike)`로 교체하고, 앱 전역 타입 주석을 `RedisLike | None`로 통일.
  **런타임 isinstance는 종료 경로(`close_redis`) 한 곳에만 남긴다** — 요청 경로의 조회 창구
  (`get_app_redis`)는 매 요청 도는 핫패스라 멤버 수에 비례하는 속성 검사를 태우지 않고,
  대입 지점(`create_redis_client`의 `client: RedisLike = Redis(...)`)의 타입 주석이
  계약 위반을 **컴파일 타임에** 잡는다. 즉 부팅은 정적으로, 종료는 런타임으로 지킨다.
- `FakeRedis`는 상속을 버리고 Protocol 멤버를 직접 구현한다.
- 로컬 스텁(`typings/`)과 `stubPath` 설정을 **삭제** — pyright가 업스트림 redis-py
  타입으로 검사한다. `cast(Any, Redis).from_url` 우회 2곳도 정타입 호출로 회귀.

Protocol 시그니처 규약: 파라미터는 positional-only(`/`)로 선언해 redis-py의
파라미터 이름(`name=…`)과의 표기 차이를 계약에서 배제하고, 반환은 `Any`
(redis-py 명령 반환이 동기/비동기 겸용 유니온 `Awaitable[T] | T`라 좁은 반환
타입은 실클라이언트와 어긋난다 — 호출부는 항상 `await`).

## 트레이드오프 (Consequences)

**얻은 것**
- pyright 0 errors가 **실제 검증 결과**가 됨 — 실클라이언트 대입
  (`client: RedisLike = Redis(...)`)이 계약 위반을 컴파일 타임에 잡는다.
- 테스트 가짜가 실클라이언트 상속·스텁 없이 성립 — 유지보수 표면 축소.
- 명령 추가 시 Protocol에 등록해야 하므로 **Redis 사용 표면이 한 파일에 명세**된다.

**치른 비용**
- `runtime_checkable` isinstance는 멤버 **존재만** 확인한다(시그니처는 pyright 몫)
  — 혈통 검사보다 얕다. 가드의 목적이 "잘못된 state 주입의 하류 AttributeError 방지"
  (fail-open 판별)이므로 존재 검사로 충분하다.
- isinstance 비용이 멤버 수(12)에 비례한다 — 그래서 요청 경로에서는 쓰지 않고(위 결정),
  멤버도 실사용 명령으로 제한한다. 대신 요청 경로는 런타임 보증 없이 타입 검사에 의존한다.

## 고려한 대안 (Alternatives)

| 대안 | 기각 사유 |
|------|-----------|
| 현상 유지(상속 + 로컬 스텁) | 스텁이 업스트림 타입을 가려 0 errors가 공허해짐. 스텁 자체가 낡음(미사용 명령 포함, `from_url` 누락으로 cast 우회 유발) |
| `fakeredis` 라이브러리 도입 | dev 의존성 +1, Lua eval 시맨틱(RENAME 스왑·CAS 해제)의 테스트 제어가 지금의 수제 가짜보다 어려움 |
| 가드 제거(덕 타이핑만) | 잘못된 `app.state.redis` 주입이 하류 AttributeError로 터짐 — fail-open 계약([ADR 0005](0005-resilience-no-circuit-breaker.md)) 위반 |

## 일부러 하지 않은 것

- **FakeRedis의 bytes 반환 유지** — 실클라이언트는 `decode_responses=True`로 str을
  반환하지만, 가짜는 일부 명령에서 bytes를 반환한다. 소비부(`bulk_to_str`·`int()`)가
  양쪽을 방어함을 확인했고, 반환형 통일은 테스트 시맨틱 변경이라 이 결정과 분리한다.
- **Protocol 반환 타입 정밀화** — `Any` 대신 명령별 정확한 반환을 선언하는 것.
  redis-py의 유니온 반환과 정합시키는 비용 대비, 호출부가 이미 방어적이라 이득이 작다.
