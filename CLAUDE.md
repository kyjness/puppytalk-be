# CLAUDE.md

PuppyTalk 백엔드에서 코딩 에이전트가 **작업 전에 반드시 읽어야 할 것**과, 저장소를 훑어서는
바로 안 보이는 규칙만 모았다. 서비스 소개·기술 스택은 [`README.md`](README.md)에 있다.

## 먼저 읽는다

| 문서 | 언제 |
|---|---|
| [`CONVENTIONS.md`](CONVENTIONS.md) | **브랜치를 만들기 전.** 브랜치 접두사·커밋 메시지·PR 형식의 정본 |
| [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md) | PR 본문을 쓰기 전. **섹션 이름을 그대로** 쓴다 |
| [`docs/adr/README.md`](docs/adr/README.md) | 설계에 손대기 전. 왜 이렇게 됐는지의 정본 |
| [`docs/backlog.md`](docs/backlog.md) | 알려진 결함·최적화 항목과 각 근거 |

특히 **`refactor/`·`perf/`는 동작 불변일 때만** 쓴다. 동작이 바뀌면 `fix/` 또는 `feature/`다.

## ADR이 코드보다 먼저다

`docs/adr/`는 장식이 아니라 **지켜야 하는 계약**이다. 코드를 바꿔 ADR의 서술과 어긋나게
됐다면 둘 중 하나다 — 코드가 틀렸거나, ADR을 같이 갱신해야 하거나. 그냥 두면 안 된다.

작업하면서 자주 걸리는 것:

- **[ADR 0015](docs/adr/0015-index-migration-concurrently.md) — 라이브 테이블 인덱스는
  `CONCURRENTLY`.** `postgresql_concurrently=True` + `op.get_context().autocommit_block()` +
  `if_not_exists=True`를 함께 쓰고, **인덱스 리비전은 다른 DDL과 섞지 않는다**(부분 적용 시
  `alembic_version`이 뒤로 남아 재실행이 깨진다).
- **인덱스는 마이그레이션과 ORM 양쪽에 쓴다.** 통합 테스트 DB는 `Base.metadata.create_all`로
  만들어지므로, 마이그레이션에만 있는 인덱스는 테스트에서 **존재하지 않는다**.
  `app/domain/comments/model.py`가 선례다.
- **[ADR 0005](docs/adr/0005-resilience-no-circuit-breaker.md) — 외부 I/O는 fail-open.**
  단, `try/except` fail-open은 **타임아웃이 유한할 때만** 성립한다. Redis 호출에 새 경로를
  추가한다면 소켓 타임아웃이 걸려 있는지 확인한다(`app/infra/redis.py`).

## 검사

CI(`.github/workflows/ci.yml`)와 **같은 poe 태스크**를 로컬에서 그대로 돌린다.
정의는 `pyproject.toml`의 `[tool.poe.tasks]`가 단일 출처다.

```bash
uv run poe check                   # lint-check + format-check + typecheck + vulture-check + audit
uv run pytest tests/unit           # DB 불필요
uv run pytest tests/integration    # PostgreSQL + MinIO 필요 (./dev.sh 로 인프라 기동)
```

로컬 스택은 `./dev.sh`(인프라만 컨테이너, 백엔드·프론트는 호스트 reload).

**CI가 있는데도 로컬에서 돌리는 이유** — CI는 브랜치 tip과 `main`·`develop` push만 검사한다.
이 레포는 squash가 아니라 **merge commit**으로 통합하므로 중간 커밋이 `develop`에 영구히
남는다. 즉 `git bisect`가 밟는 것은 CI가 **한 번도 본 적 없는** 커밋들이다. 커밋 각각이
단독으로 통과하는지는 그 시점을 checkout해 로컬에서 돌리는 수밖에 없다.

## 테스트를 쓸 때

결함을 고쳤다면, **그 테스트가 수정 전 코드에서 실제로 실패하는지 먼저 확인한다.**
확인하지 않은 회귀 테스트는 아무것도 증명하지 않는다. PR 체크리스트에도 있는 항목이다.
