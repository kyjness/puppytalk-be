# ADR 0015 — 인덱스 마이그레이션: 라이브 테이블은 CONCURRENTLY

- **상태**: 채택됨 (Accepted)
- **관련 코드**: `migrations/versions/*`, `migrations/script.py.mako`,
  `app/domain/*/model.py`(`__table_args__` 인덱스 선언)
- **관계**: [00 운영 봉투](../00-operating-envelope-and-scope.md)의 *배포 = 무중단 롤링 /
  블루-그린*, *가용성 99.9%* 전제를 마이그레이션 층에서 지킨다.

## 맥락 (Context)

지금까지의 인덱스 마이그레이션은 전부 평범한 `op.create_index(...)`다 — 11개 리비전에
`postgresql_concurrently`가 한 번도 쓰이지 않았다.

PostgreSQL의 비-`CONCURRENTLY` `CREATE INDEX`는 대상 테이블에 **SHARE 락**을 잡는다.
읽기는 통과하지만 **INSERT·UPDATE·DELETE가 인덱스 빌드 내내 차단**된다. `posts.title`·
`posts.content`의 pg_trgm GIN처럼 무거운 인덱스면 라이브 테이블에서 사실상 쓰기 중단이고,
"무중단 롤링 배포"·99.9%와 정면으로 충돌한다.

지금까지 사고가 없었던 건 설계가 좋아서가 아니라 **구축기라 테이블이 비어 있었기**
때문이다. 빈 테이블의 인덱스 빌드는 순간이라 락 구간이 보이지 않는다. 데이터가 쌓인
뒤 같은 습관으로 인덱스를 추가하면 그때 처음 드러난다 — 가장 나쁜 시점에.

**기존 리비전은 고치지 않는다.** 이미 적용된 마이그레이션은 다시 실행되지 않으므로
소급 수정은 실행되지 않는 코드를 바꾸는 일이고, 당시엔 실제 피해도 없었다.
이 ADR의 산출물은 **앞으로의 규약**이다.

## 결정 (Decision)

**데이터가 있는 테이블에 인덱스를 추가·삭제하는 마이그레이션은 `CONCURRENTLY`로 한다.**

```python
def upgrade() -> None:
    # CONCURRENTLY는 트랜잭션 안에서 실행할 수 없다 — Alembic이 기본으로 감싸는
    # 트랜잭션을 autocommit_block으로 잠시 벗어난다.
    with op.get_context().autocommit_block():
        op.create_index(
            "idx_example",
            "posts",
            ["created_at"],
            unique=False,
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index("idx_example", table_name="posts", postgresql_concurrently=True)
```

규약 요소는 넷이다.

1. **`postgresql_concurrently=True`** — 빌드 중 쓰기를 막지 않는다. 대가는 테이블을 두 번
   스캔해 더 오래 걸린다는 것. 무중단이 우선이다.
2. **`autocommit_block()` 필수** — Alembic은 마이그레이션을 트랜잭션으로 감싸는데
   `CREATE INDEX CONCURRENTLY`는 트랜잭션 블록 안에서 실행할 수 없다. 빠뜨리면
   `CREATE INDEX CONCURRENTLY cannot run inside a transaction block`으로 **배포가 깨진다**.
   이게 이 규약에서 가장 자주 밟는 지뢰다.
3. **`if_not_exists=True`** — 아래 실패 복구 절차와 짝을 이룬다. 재시도가 안전해진다.
4. **`downgrade`도 `CONCURRENTLY`** — 롤백이 쓰기를 멈추면 롤백이 2차 장애가 된다.

### 실패 시 운영 절차

`CREATE INDEX CONCURRENTLY`는 실패해도 **INVALID 상태의 인덱스를 남긴다**. 이 인덱스는
플래너가 쓰지 않으면서 쓰기 비용만 유발하므로, 재시도 전에 반드시 정리한다.

```sql
-- 1) INVALID 인덱스 확인
SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;
-- 2) 제거 후 마이그레이션 재실행
DROP INDEX CONCURRENTLY <index_name>;
```

### 예외 — 규약을 적용하지 않는 경우

- **최초 베이스라인**(`001_initial_baseline`)과 새로 만드는 테이블의 인덱스 — 테이블이
  비어 있어 락 구간이 없다. `CREATE TABLE`과 같은 트랜잭션에 두는 편이 원자적이라 낫다.
- **시드·룩업 테이블**(예: `categories`) — 행이 수십 개 규모라 빌드가 순간이다.

판정 기준은 "이 테이블에 운영 데이터가 쌓여 있는가" 하나다. 애매하면 `CONCURRENTLY`를 쓴다.

## 트레이드오프 (Consequences)

**얻은 것**
- 인덱스 추가가 배포 중 쓰기를 멈추지 않는다 — 무중단 롤링 전제가 마이그레이션 층까지 이어진다.
- 실패해도 재시도가 안전하다(`if_not_exists` + INVALID 정리 절차).

**치른 비용**
- 빌드가 느리다(테이블 2회 스캔). 큰 테이블에서는 배포 창이 길어진다.
- 마이그레이션이 트랜잭션 밖에서 돌아 **부분 적용이 가능하다** — 한 리비전에 여러 DDL을
  섞으면 중간 실패 시 상태가 애매해진다. 그래서 인덱스 리비전은 **다른 DDL과 섞지 않는다**.
- `autocommit_block`을 빠뜨리면 배포가 깨진다는 함정이 남는다 — 그래서 이 문서가 있다.

## 고려한 대안 (Alternatives)

| 대안 | 기각 사유 |
|------|-----------|
| 지금처럼 일반 `CREATE INDEX` 유지 | 데이터가 쌓인 뒤 첫 인덱스 추가에서 쓰기 중단 — 무중단 전제 위반 |
| 인덱스 추가를 마이그레이션 밖 수동 작업으로 | 스키마 이력이 코드 밖으로 새고 환경 간 드리프트가 생긴다 |
| 점검 창(maintenance window)을 잡아 일반 인덱스 생성 | 무중단 롤링/블루-그린 전제를 포기하는 선택 — 봉투와 어긋난다 |
| `pg_repack` 등 외부 도구 도입 | 인덱스 추가 하나에 운영 도구 의존을 늘리는 건 봉투 대비 과잉 |

## 일부러 하지 않은 것 (Non-goals)

- **기존 11개 리비전 소급 수정**: 적용된 마이그레이션은 재실행되지 않아 의미가 없다.
- **`CONCURRENTLY` 자동 강제(린터·훅)**: 예외(빈 테이블·베이스라인)가 정당하게 존재해
  기계적 강제는 오탐을 만든다. 판정은 사람이 하고 근거는 이 문서가 준다.
- **온라인 스키마 변경 일반화**(컬럼 타입 변경 등의 무중단 절차): 이번 범위는 인덱스다.
  필요해지면 그때 별도 ADR로 다룬다.
