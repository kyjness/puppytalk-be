# PuppyTalk Conventions

git 워크플로 규약. 아키텍처 결정은 [`docs/adr/`](docs/adr/README.md)가, 진행 상황은
[`docs/ROADMAP.md`](docs/ROADMAP.md)가 담당한다. 여기는 **브랜치·커밋·PR**만 다룬다.

## 브랜치 (git-flow)

- **`main`** — 프로덕션. 라이브 데모가 도는 것([ADR 0017](docs/adr/0017-demo-deployment-topology.md)).
  `develop`을 승격해서만 전진한다.
- **`develop`** — 통합 대상. 모든 작업은 여기로 먼저 PR을 통해 머지된다.
- 작업 브랜치는 짧게 살고 PR로 `develop`에 들어간다.

CI(`ci.yml`)는 `main`·`develop` push와 **모든 PR**에서 돈다.

### 브랜치 이름

`<type>/<short-kebab-description>` — 영문 kebab-case.

| Type        | 용도                          | 예시                                    |
|-------------|-------------------------------|-----------------------------------------|
| `feature/`  | 새 기능                       | `feature/comment-pagination`            |
| `fix/`      | 버그 수정                     | `fix/redis-socket-timeout`              |
| `perf/`     | 동작 불변 성능 개선           | `perf/chat-room-cache`                  |
| `refactor/` | 동작 불변 구조 개선           | `refactor/media-service-split`          |
| `docs/`     | 문서만                        | `docs/architecture-pipeline-diagrams`   |
| `ci/`       | CI/CD 파이프라인              | `ci/cd-skip-without-deploy-secrets`     |
| `chore/`    | 유지보수·툴링                 | `chore/dependabot-target-develop`       |

- **`feature/`를 쓴다(`feat/` 아님).** `feat`은 [Conventional Commits](https://www.conventionalcommits.org)의
  *커밋 타입*이고, *브랜치 접두사*의 정본은 git-flow의 `feature/`다. 두 명세가 다른 대상을
  가리키므로 각자의 표기를 따른다 — 커밋은 `feat:`, 브랜치는 `feature/`.
- 이 규약 이전 브랜치는 `feat/`을 썼다(`feat/comment-default-popular-sort` 등). 과거 이력은
  그대로 두고 신규만 `feature/`를 적용한다. 브랜치는 머지되면 사라지므로 전환 비용이 없다.

## 커밋 메시지

**Conventional Commits 형식 + 한국어 본문.**

```
<type>(<scope>): <제목 — 무엇을 왜>

<본문: 왜 이 변경이 필요했는지, 어떤 판단을 했는지. 무엇을 바꿨는지는 diff가 말한다.>
```

- **타입**: `feat` · `fix` · `perf` · `refactor` · `docs` · `test` · `ci` · `chore`
- **scope**: 도메인/영역. 생략 가능. `chat` · `comments` · `media` · `redis` · `ops` · `cd` 등
- **제목**: 한국어, 마침표 없음. **결과를 서술한다** — "WS 종료 코드를 사유별로 분리",
  "배포 시크릿이 없으면 실패 대신 건너뛴다". "수정", "개선" 같은 빈 동사는 쓰지 않는다.
- **본문**: 결정의 근거를 남긴다. 왜 다른 방식이 아니라 이 방식인지, 어떤 트레이드오프를
  받아들였는지. 관련 ADR·이슈 번호를 인용한다.

### 형식 — 훑어읽을 수 있어야 한다

내용이 옳아도 **한 문단에 몰아 쓰면 아무도 안 읽는다.** 판정 기준은 하나다:
**30초 훑어서 "무엇이 왜 문제였고 어떻게 고쳤나"가 잡히는가.**

- **제목**: 50자 내외. 넘으면 scope를 쓰거나 본문으로 내린다.
- **본문은 문단이 아니라 불릿**. 산문 6줄짜리 덩어리를 만들지 않는다.
  한 불릿 = 한 사실. 인과는 `→` 로 잇는다.
- **변경이 둘 이상이면 소제목으로 나눈다**(`## 1) …`). 번호만 붙인 긴 문단은 안 된다.
- **대비는 표로**. "전에는 A, 지금은 B"는 문장보다 2열 표가 빠르게 읽힌다.
- 한 줄 **72자**에서 자른다. `git log`가 들여쓰기를 더해도 안 넘친다.
- 블록 사이에 **빈 줄**을 넣는다.
- 강조는 아껴 쓴다 — 문단마다 굵게가 있으면 아무것도 강조되지 않는다.

읽히는 형태:

```
fix(redis): 소켓 타임아웃 — 먹통 Redis에서 fail-open이 발동하도록

## 문제

- 모든 fail-open이 `except` 기반인데 클라이언트에 타임아웃이 없다
- Redis **죽음** → connection refused → 예외 → 정상 동작
- Redis **먹통**(분단·페일오버) → 패킷 소실 → 예외가 안 옴 → 폴백 미실행
- 인증 요청은 Redis를 3회 경유하고 앞의 둘은 핸들러 전 → 전 요청 정지

## 수정

- 클라이언트 3곳에 `socket_timeout`(1s)·`socket_connect_timeout`(2s)
- 구독 소켓만 폴 간격의 5배 — 유휴가 정상이라 짧으면 매초 끊긴다

근거는 ADR 0005에 명문화. 테스트는 수정 전 코드에서 실패함을 확인했다.
```

### 커밋을 잘게 쪼개지 않는다

**기본은 한 작업 = 한 커밋**이다. 같은 작업의 **코드·테스트·문서는 한 커밋에 함께** 간다 —
파일 종류나 변경 성격(fix/docs)은 분할 사유가 아니다.

나누는 것은 *정말 필요할 때*만이다:
- 서로 **무관한** 결함을 한 브랜치에서 같이 고쳤을 때
- 한쪽만 되돌려야 할 실질적 이유가 있을 때

## PR

- **base는 `develop`** (릴리스 PR `develop → main` 제외).
- 제목·본문 모두 커밋 메시지와 **같은 형식 규칙**을 따른다(위 참조).
- 절 이름은 [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md)를
  그대로 쓴다 — **Description · Changes Made · Testing · Related · Checklist**.
  업계 관례를 따르는 이름이라 밖에서 온 리뷰어도 바로 읽는다. 임의로 바꾸지 않는다.
- **Description은 "왜"로 시작한다.** "무엇을 바꿨나"는 Changes Made와 diff가 말한다.
  이 레포는 이슈 트래커를 쓰지 않으므로 맥락을 담을 티켓이 없다 — PR이 그 역할을 진다.
  결함 수정이라면 **재현 조건과 영향 범위**를 먼저 적는다.
- 검증 결과(테스트 수·정적 검사)를 Testing에 남긴다. 결함 수정이면 **그 테스트가 수정 전
  코드에서 실패하는 것을 확인했는지**도 적는다 — 통과만 보고한 테스트는 아무것도 증명하지 않는다.
- 선행 조건이 있으면(다른 PR 머지·배포 등) Description 최상단에 경고로 둔다.
- 해당 없는 절은 지운다. 빈 절을 남기면 템플릿이 잡음이 된다.

### 분량은 영향 범위에 비례한다

긴 본문이 항상 좋은 게 아니다. **작은 수정에 긴 본문은 오히려 판단력 부족으로 읽힌다.**

| PR 성격 | 적정 분량 |
|---|---|
| 오타·상수 변경 | 제목 한 줄. 본문 없어도 된다 |
| 일반 버그 수정 | Description 2~3줄 + Testing |
| 기능 추가 | 전 절을 쓰되 각각 짧게 |
| 아키텍처·마이그레이션·다건 결함 | 소제목·표를 동원한 긴 본문이 정당하다 |

## 버전 태그

아직 쓰지 않는다. 라이브 데모가 단일 인스턴스 수동 승격이라 릴리스 경계가 없다
([ADR 0017](docs/adr/0017-demo-deployment-topology.md)). 도입한다면 `main`에 SemVer
annotated 태그(`v1.0.0`)로 하고, 그때 이 절을 갱신한다.
