# ADR 0017 — 라이브 데모 배포: 단일 인스턴스 compose (관리형 서비스 미사용)

- **상태**: 채택됨 (Accepted) · 2026-08-05
- **관련 코드**: `compose.prod.yml`, `Caddyfile`, `.env.prod.example`,
  `app/scripts/seed_demo.py`, `scripts/backup_db.sh`, `.github/workflows/cd.yml`,
  `puppytalk-infra/demo/`(terraform root)

## 맥락 (Context)

설계는 코드로 있는데 **돌아가는 URL이 없었다.** `puppytalk-infra/terraform/`에는 운영 등급
토폴로지(ALB · ECS Fargate · RDS Multi-AZ · ElastiCache · NAT×2)가 기술돼 있지만, 이 스택을
`apply`하면 트래픽이 0이어도 **월 $150~200**이 청구된다. 상시 과금을 감당할 이유가 없어
적용하지 않기로 이미 정해 뒀고(인프라 README), 그 결과 공개된 실행 인스턴스가 없었다.

앱이 배포처에 요구하는 것은 뚜렷하다.

- **장시간 연결** — DM은 WebSocket(`app/domain/chat/manager.py`가 프로세스 메모리에 세션 보유),
  알림은 SSE. 유휴 시 잠드는 실행 모델과 맞지 않는다.
- **상주 프로세스** — Celery 워커와 lifespan 주기 작업(조회수 flush · 미디어 정리).
- **Redis · PostgreSQL 상시 가동** — 캐시·레이트리밋·pub/sub·조회수 버퍼가 전부 Redis 위에 있다.

즉 "요청당 과금" 모델(Lambda 등)로는 요구를 못 맞추고, 맞추려 해도 상태 저장 계층을 관리형으로
사서 붙여야 해 **더 비싸진다**.

## 결정 (Decision)

**배포 등급을 "쇼케이스"로 명시하고, 관리형 서비스 없이 단일 인스턴스 compose로 돌린다.**

1. **Lightsail 2GB 한 대**에 `compose.prod.yml` 전부 — Caddy · API(gunicorn 2) · Celery 워커 ·
   PostgreSQL · Redis. 실측 유휴 메모리 합계 414MiB.
2. **관리형 데이터 계층을 쓰지 않는다** — RDS 대신 postgres 컨테이너, ElastiCache 대신 redis
   컨테이너. RDS가 파는 것은 성능이 아니라 관리(백업·패치·페일오버)이므로, 이 축소로 잃는 것은
   **쿼리 성능이 아니라 운영 안전망**이다.
3. **ALB 대신 Caddy**가 TLS를 종단한다(Let's Encrypt 자동 발급·갱신).
4. **미디어만 실제 S3** — presigned POST URL이 `S3_ENDPOINT_URL` 기준으로 만들어져 브라우저에게
   그대로 전달되므로(`app/infra/storage.py`), MinIO를 인스턴스 안에 두면 그 엔드포인트를 외부에
   노출·프록시해야 한다. 실제 S3면 코드 분기 없이(ADR 0010의 단일 경로) 그대로 동작한다.
5. **프론트는 S3 + CloudFront**, apex 도메인 하나로 정적 파일과 `/media/*`를 함께 서빙한다.
6. **데모 데이터를 심는다**(`app/scripts/seed_demo.py`) — 빈 사이트에서는 커서 페이지네이션·
   조회수·트렌딩·DM·알림 중 무엇도 보여줄 수 없다.

## 트레이드오프 (Consequences)

**얻은 것**

- 월 ~$12(하루 $0.4 수준). 운영 설계 스택 대비 1/12이라 크레딧으로 수개월 유지된다.
- 콜드 스타트가 없다 — 링크를 누르면 즉시 뜬다.
- **이식성**: compose + `.env.prod` + 시드 스크립트가 전부라, 다른 클라우드로 옮길 때 바뀌는 것은
  DNS A 레코드 하나다.

**잃은 것 (의도적)**

- **HA 없음** — 인스턴스가 죽으면 사이트가 죽는다. 재시작이 복구 절차 전부다.
- **자동 백업·PITR 없음** — `scripts/backup_db.sh`가 하루 한 번 pg_dump를 S3로 올린다(14일 보관).
  그 사이 유실은 감수한다.
- **무중단 배포 없음** — `compose up -d`가 컨테이너를 갈아끼우는 동안 수 초 끊긴다.
- **Lightsail은 정지해도 과금된다**(EC2와 다르다). 잠시 내리려면 스냅샷 후 인스턴스를 지워야 한다.

## 고려한 대안 (Alternatives)

핵심은 **"무엇이 서버리스가 되는가"** 다. 이 앱에서 돈을 쓰는 쪽은 요청 처리가 아니라 **항상 켜져
있어야 하는 상태 계층(PostgreSQL·Redis)** 이다. 실행 계층만 종량제로 바꾸는 선택지들은 그 상태
계층을 관리형으로 사게 만들어, 실행 비용을 아낀 것보다 더 큰 고정비를 새로 만든다.

| 대안 | 왜 안 썼나 |
|---|---|
| `terraform/` 운영 스택 그대로 apply | 월 $150~200. 트래픽 0인 데모에 정당화 불가 |
| **ECS Fargate** | 서버리스가 되는 건 **컨테이너 실행뿐**이다. Fargate 태스크에는 영속 볼륨이 없어 PostgreSQL·Redis를 함께 담을 수 없고(EFS를 붙여도 DB를 얹을 물건이 아니다), 결국 **RDS + ElastiCache를 사야 한다**. 인터넷 노출에는 ALB(월 ~$16)가, 프라이빗 배치에는 NAT(월 ~$32)가 더 붙는다. 태스크 자체는 0.25 vCPU 기준 월 ~$9로 싸지만 **합계는 월 $60 이상**이다. 얻는 것(롤링 무중단 배포·오토스케일·노드 관리 없음)은 트래픽이 0인 데모에서 값을 못 한다 — 그 설계가 필요해지는 규모의 답은 `terraform/` 스택에 이미 코드로 있다 |
| **Lambda + API Gateway** | 구조적으로 불가능하다. API Gateway는 응답을 버퍼링해 **SSE가 죽고**(Function URL의 응답 스트리밍은 Node.js 런타임 전용), WebSocket은 커넥션 저장소를 둔 전면 재작성이 필요하다. Celery 워커와 lifespan 주기 작업도 각각 SQS·EventBridge로 쪼개야 한다. 그러고도 RDS·ElastiCache(+커넥션 폭증 방지용 RDS Proxy)를 사야 해 **월 $40~55** |
| **EC2 t4g.small** | 구조는 지금과 똑같은데 월 ~$22다. 고정 IP·디스크·전송이 요금에 포함된 Lightsail이 절반값이다. 대가로 IAM 인스턴스 롤을 잃었다(아래) |
| Fly/Render + Neon + Upstash 조합 | Celery 브로커 폴링과 SSE pub/sub이 Upstash 무료 명령 한도를 빠르게 소진한다. 피하려면 `CELERY_ENABLED=false`로 두어야 하는데, 그러면 이 프로젝트의 핵심 경로를 데모에서 못 보여준다 |

> 정리하면 **VM 한 대에 다 몰면 PostgreSQL·Redis 비용이 0이 된다.** 실행 계층을 종량제로 바꾸는
> 모든 대안은 이 둘을 유료 관리형으로 되사야 해서, 더 비싸지거나(Fargate·Lambda) 무료 한도에
> 부딪힌다(Upstash).

## 일부러 하지 않은 것 (Deliberately not done)

- **SSM 무키 배포를 포기했다.** Lightsail에는 IAM 인스턴스 롤을 붙일 수 없어
  `GitHub OIDC → ssm:SendCommand` 경로를 쓸 수 없다. CD는 SSH 키로 하며(`cd.yml`), 배포 전용
  개인키 하나가 GitHub Secrets에 상주한다. **프론트 배포는 그대로 OIDC**를 쓴다.
  운영 등급 CD 설계(`terraform/iam_github_oidc.tf`)는 스택에 그대로 남아 있다.
- **시크릿을 IaC에 넣지 않았다.** `demo/` terraform은 JWT 키·DB 비밀번호·S3 액세스 키를 모른다 —
  전부 서버의 `.env.prod`로만 들어간다. tfstate·인스턴스 메타데이터에 평문 시크릿이 남지 않는
  대신, 최초 1회 수동 배치와 액세스 키 수동 발급이 필요하다.
- **모니터링·알람을 두지 않았다.** `/metrics`(ADR 0006)는 그대로 뜨지만 Prometheus·Grafana를
  띄우지 않았다. 2GB에서 관측 스택이 앱보다 무거워지고, 볼 사람이 없는 대시보드다.
- **멀티아키텍처 이미지를 만들지 않았다.** Lightsail은 x86이라 지금은 amd64 하나면 된다.
  ARM(오라클 Always Free 등)으로 옮기는 시점에 `ci.yml`의 docker 잡에
  `platforms: linux/amd64,linux/arm64` 한 줄을 추가한다 — 미리 넣으면 빌드 시간만 두 배가 된다.
