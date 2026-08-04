# ADR 0007 — 조회수 집계: Redis 버퍼링 + 비동기 Flush

- **상태**: 채택됨 (Accepted)
- **관련 코드**:
  `app/domain/posts/services/post_service.py`
  (`_consume_view_if_new_redis`·`_try_view_increment_in_buffer`·`flush_view_counts_to_db`·`_merge_drain_into_buffer`),
  `app/domain/posts/repository.py`(`increment_view_count_delta`·`increment_view_count`),
  `app/main.py`(`_view_buffer_flush_loop` — lifespan 주기 루프)

## 맥락 (Context)

인기글 1건이 [운영 봉투](../00-operating-envelope-and-scope.md)상 **초당 수백~수천 조회**를 받는다.
조회마다 `UPDATE posts SET view_count = view_count + 1`을 치면 봉투를 못 버틴다.

1. **단일 행 write 폭주** — 같은 인기글 행에 초당 수천 UPDATE가 몰리면 행 잠금 경합으로
   직렬화된다. 조회(읽기)가 write 경합에 발목 잡히는 건 봉투에서 최악의 조합이다.
2. **write 증폭** — 조회는 읽기 경로인데 매번 write·WAL·인덱스 갱신·dead tuple을 유발한다.
3. **정확도의 가치가 낮다** — 조회수는 근사값이어도 UX가 성립한다(초당 오차는 무의미).
   강정합성을 지불할 이유가 없는 대표적 지표다.

동시에, **한 사용자의 연타·새로고침이 조회수를 부풀리는 것**은 막아야 한다(dedup).

## 결정 (Decision)

**조회수를 Redis에 write-behind 버퍼링하고, 주기적으로 배치 flush**한다.

1. **뷰어 dedup (SET NX)** — `view:post:{post_id}:viewer:{viewer_key}`에 `SET NX EX=TTL`.
   실패(이미 존재)면 증가하지 않는다. `viewer_key`는 로그인 시 `u:{user_id}`, 아니면 `ip:{client}`.
   TTL은 `VIEW_CACHE_TTL_SECONDS`(기본 3600)이고, **0 이하 = dedup 끔**(같은 viewer도 매 조회
   집계)이 유일한 예외 모드다 — 로컬/데모에서 증가를 즉시 확인하는 용도이며, 운영에서 켜면
   새로고침 루프만으로 조회수·트렌딩 점수가 인플레이션되므로 배포 템플릿(.env.example)은
   이 값을 설정하지 않는다(기본 3600).
2. **버퍼 누적 (HINCRBY)** — 새 조회는 `HINCRBY views:{v}:buffer {post_id} 1`로 Redis 해시에
   쌓기만 한다. **읽기 경로에서 DB write가 사라진다.**
3. **주기 flush (asyncio 루프 + 분산락)** — 각 인스턴스가 lifespan에서 `_view_buffer_flush_loop`를
   돌린다. flush는 `SET NX`로 **분산 락**을 잡아 *틱당 인스턴스 1대만* 실제 flush하고(3~10대
   동시 실행 방지), 락은 랜덤 토큰 값 + **Lua CAS(GET==value일 때만 DEL)**로 해제한다 —
   TTL 만료 후 다른 워커가 재획득한 락을 실수로 지우지 않는다.
4. **원자적 drain (RENAME)** — flush는 버퍼를 `RENAME`으로 `drain:{ulid}`에 스왑한 뒤 집계한다.
   집계 중 유입되는 새 조회는 *새* 버퍼에 쌓이므로 **유실 없이** 다음 틱에 반영된다.
5. **배치 반영** — drain의 각 `{post_id: delta}`를 `increment_view_count_delta(post_id, delta)`
   단일 UPDATE로 합산 반영. 초당 수천 조회가 한 틱에 **행당 1 UPDATE**로 접힌다.
6. **fail-open** — Redis 장애 시 dedup·버퍼가 모두 예외를 삼키고, 조회는 DB 직접 증가로 폴백한다
   ([ADR 0004](0004-cache-strategy.md)·[0005](0005-resilience-no-circuit-breaker.md) 표준).
7. **flush 실패 복구** — DB 반영 중 오류면 drain을 **버퍼로 되돌려(HINCRBY 재병합)** 유실을 막고
   예외를 올린다. 다음 틱에서 재시도된다.

> `{v}` 는 Redis Cluster **해시 태그**다(#17). buffer·flush 락·drain 키가 같은 슬롯에 놓여야
> 락 보호 하에 `RENAME`(교차 슬롯 불가)이 성립한다. 반면 뷰어 dedup 키는 슬롯을 공유할 필요가
> 없어(독립 `SET NX`) 일부러 해시 태그를 두지 않는다.

## 트레이드오프 (Consequences)

**얻은 것**
- 읽기 폭주 경로에서 **행 잠금 경합·write 증폭 제거** — 인기글 조회가 DB write에 안 막힌다.
- 초당 수천 조회 → 틱당 행 1 UPDATE로 **write 접기**.
- Redis 장애에도 조회는 계속된다(fail-open, DB 폴백).

**치른 비용**
- **근사 즉시성** — 화면 조회수가 flush 주기(`VIEW_BUFFER_FLUSH_INTERVAL_SECONDS`)만큼 지연.
  상세 응답은 `DB view_count + Redis 버퍼 pending`을 합쳐 보정해 체감 지연을 줄인다.
- **미반영 창** — flush 직전 인스턴스 크래시 시 마지막 버퍼분 유실 가능(근사 지표라 허용).
- **랭킹 반영 지연** — 조회수는 트렌딩 점수의 주요 신호([ADR 0004](0004-cache-strategy.md))인데,
  랭킹은 DB 값만 읽고 Redis 버퍼 pending은 보지 않는다. 따라서 조회 1건이 순위에 반영되기까지
  flush 주기(300s) + 트렌딩 캐시 TTL(300s) = **최대 약 10분**이 걸린다. 상세 응답과 달리 여기서
  pending을 합치지 않는 이유는, 랭킹이 SQL `ORDER BY`로 산정돼 보정하려면 넓은 후보를 뽑아
  애플리케이션에서 재정렬해야 하기 때문이다. 24h 집계 창에서 10분은 무의미한 수준이고
  인기글 위젯은 실시간성보다 안정성이 중요하므로, 정확도가 아니라 **지연을 택했다**.
  flush 주기 단축은 DB write 빈도만 늘고 체감 이득이 없어 하지 않는다.
- 운영 복잡도 — 버퍼·drain·분산락·재병합이라는 상태 기계가 늘었다(그래서 이 ADR·단위 테스트로 근거화).

## 고려한 대안 (Alternatives)

| 대안 | 기각 사유 |
|------|-----------|
| 조회마다 DB `UPDATE +1` | 인기글 단일 행 잠금 경합·write 증폭 → 봉투(초당 수천 조회) 붕괴 |
| DB 원자 증가 + 뷰어 dedup만 | write 폭주는 그대로 — dedup은 중복만 줄일 뿐 절대량을 못 접는다 |
| Kafka/스트림으로 이벤트 집계 | 봉투(수천~수만 DAU) 대비 과잉 — 인프라·운영 비용이 이득을 초과 |
| Celery 태스크로 flush | 브로커 왕복·가시성 타임아웃 관리 추가. 주기 flush는 인스턴스 내 asyncio 루프 + 분산락으로 충분 |
| `SET NX` 없이 delete로 락 해제 | TTL 만료 후 남의 락을 삭제해 이중 flush → 조회수 이중 반영(#2). CAS로 해결 |

## 일부러 하지 않은 것 (Non-goals)

- **조회수 exactly-once**: 근사 지표에 불필요. 크래시 시 마지막 버퍼분 유실·재시도 중복 가능성을 허용.
- **per-view 즉시 정확 반영**: 실시간 정확 조회수는 봉투 밖 요구. flush 주기 근사로 충분.
- **뷰어별 조회 로그/분석 파이프라인**: 집계 수치만 유지, 개별 이벤트 원장은 남기지 않는다.
- **멀티리전 조회수 합산**: 단일 리전 전제([00](../00-operating-envelope-and-scope.md) 상한 밖).
