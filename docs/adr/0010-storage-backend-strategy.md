# ADR 0010 — 스토리지 백엔드 전략: S3 API 단일 경로 + dev MinIO 패리티

- **상태**: 채택됨 (Accepted) · **구현 완료(Transition/Ops)** — 아래 구현 노트
- **관련 코드**: `app/infra/storage.py`(S3 클라이언트·addressing style · presigned 계열
  `issue_presigned_post`/`promote_pending_object`/`require_s3_direct_upload`),
  `app/domain/media/service.py`, `app/domain/media/router.py`

## 맥락 (Context)

현재 스토리지는 `STORAGE_BACKEND=local|s3`로 분기하고, 미디어 업로드 **경로가 둘**이다.

1. **Presigned 직접 업로드** (`/media/images/presign` → 클라이언트가 스토리지로 직접 PUT →
   `/confirm`) — `require_s3_direct_upload()`가 `local`이면 `raise`. **S3에서만 동작한다.**
2. **직접 multipart** (`/media/images`·`/media/images/signup` → 앱 서버가 수신·저장) — local·s3 모두.

[운영 봉투](../00-operating-envelope-and-scope.md)의 멀티 인스턴스(3~10대)·업로드 부하에서
**경로 ①이 운영 등급 선택**이다: 업로드 대역폭을 앱 서버에서 떼어 스토리지로 직접 보낸다. 그런데
`local` 개발 환경에서는 **경로 ①을 실행조차 못 한다** — 즉 prod의 주 업로드 경로가 dev/CI에서
검증되지 않는다. 게다가 `local` 디스크 백엔드는 prod에 존재하지 않는 **두 번째 구현**이라, S3에서만
드러나는 버그(키 프리픽스·presign·copy 승격)를 가릴 수 있다.

## 결정 (Decision)

**"S3 API를 단일 스토리지 계약으로, dev/CI는 S3 호환 MinIO로 패리티"** 를 채택한다.

1. **S3 API 단일 경로** — presigned 직접 업로드(①)를 기본 업로드 경로로 삼는다. prod·dev·CI 모두
   동일한 boto3/S3 코드 경로를 탄다.
2. **dev/CI = MinIO** — S3 호환 오브젝트 스토리지 MinIO를 개발·테스트 백엔드로 쓴다(무료·self-host,
   presigned POST·`copy_object` 승격 지원). prod = 실제 S3. 코드 분기 없이 엔드포인트·자격만 다르다.
3. **local 디스크 백엔드 폐기** — `_local_save`/`_local_delete`와 `STORAGE_BACKEND=local` 분기를
   제거한다. 단, **MinIO가 dev/CI에 배선되기 전까지는 남겨 둔다**(그 사이 개발·테스트가 깨지지
   않도록). 제거 시점 = Ops 단계에서 docker-compose에 MinIO를 올린 직후.
4. **배선은 Transition(Ops)** — docker-compose·CI에 MinIO 컨테이너 추가, 통합 테스트를 MinIO 대상
   실행으로 전환, 그 후 local 백엔드 코드 삭제. 이 ADR은 **방향 확정**이고, 코드 반영은 Ops 묶음에서.
5. **이 스토리지와 DB를 어떤 순서로 쓸지는 [ADR 0019](0019-storage-db-write-ordering.md)에서 정한다.**
   요약: 요청 경로는 DB만 변경하고 스토리지 변경은 스위퍼가 맡는다. 근거와 대안은 그쪽에 있다.

> 비용: 포트폴리오는 유료 S3 버킷을 상시 켤 필요가 없다. dev·데모는 MinIO(무료, 앱과 같은
> compose/ECS에 동거 가능), 실제 S3는 설정·문서로만 두고 진짜 AWS 배포 시에만 과금한다.

## 트레이드오프 (Consequences)

**얻은 것**
- **dev/prod parity** — dev/CI가 prod와 같은 S3 코드 경로를 실행. presigned 업로드(①)가 비로소
  자동 검증된다.
- **단일 구현** — prod에 없는 divergent local 경로 제거로 유지보수·버그 은폐 표면 축소.
- 비용 0으로 운영 경로 시연 가능(MinIO).

**치른 비용**
- 로컬 실행·CI에 **MinIO 컨테이너 의존** — 도커 없이 즉석 실행하던 편의를 잃는다.
- 통합 테스트 인프라가 Redis·Postgres에 이어 **MinIO까지** 필요(compose로 일괄 기동해 완화).

## 고려한 대안 (Alternatives)

| 대안 | 기각 사유 |
|------|-----------|
| local-only dev 유지 | presigned 주 경로를 dev/CI에서 영영 검증 못 함 — 가장 약한 선택 |
| local 백엔드 영구 병행 | prod에 없는 이중 구현 유지 — divergence로 S3 전용 버그 은폐 |
| dev도 실제 S3 | AWS 계정·비용을 개발 루프에 결합. 무료 MinIO로 동일 API 검증 가능 |
| 멀티 클라우드 스토리지 추상화 | 봉투에 GCS·Azure 요구 없음 — 과한 추상화 |

## 일부러 하지 않은 것 (Non-goals)

- **prod에서 MinIO 자체 운영**: prod는 관리형 S3. MinIO는 dev/CI/데모 한정(자체 스토리지 서버
  운영은 가용성·백업 부채).
- **매직바이트(바이트 레벨) 이미지 검증**: direct 업로드 제거로 서버가 파일 본문을 받지
  않으므로, 바이트 검증을 하려면 confirm에서 객체를 내려받아야 한다(업로드 대역폭을 앱에서
  떼어낸 이 ADR의 목적과 상충). 방어는 presign의 Content-Type 정책 조건 + confirm의
  head 기반 재검증 + 확장자를 Content-Type에서 강제 + 응답의 `X-Content-Type-Options:
  nosniff`로 한정한다 — "이미지 아닌 바이트가 .png URL로 서빙될 수 있음"은 의식적
  트레이드오프(수용: 조회 전용 공개 버킷, 스크립트 실행 없음).
- **멀티 클라우드/스토리지 플러그인 프레임워크**: 대상이 S3 하나 → 얇은 분기로 충분.
- **CDN·이미지 리사이즈/트랜스코딩 파이프라인**: 봉투 밖(전송 최적화·미디어 가공은 이번 범위 아님).
- ~~**경로 ②(직접 multipart) 즉시 제거**: 서버 수신 업로드의 단순 경로로 당분간 병행.~~ →
  **제거 완료** — 아래 구현 노트 "direct multipart 제거" 참조.

## 구현 노트 (Transition/Ops)

- **local 백엔드 제거**: `_local_save`/`_local_delete`·`require_s3_direct_upload`·
  `STORAGE_BACKEND` 분기·config 필드·`/upload` 정적 mount를 모두 걷어내 S3 단일 경로로.
  prod 설정 검증에 S3 자격 필수 조건 추가.
- **숨어 있던 S3 전용 버그 정정**: `_get_s3_client`가 `endpoint_url`만 세팅하고 addressing style을
  안 줘서, MinIO는 virtual-hosted(`bucket.host`) DNS 미해석으로 put/presign/copy가 깨졌다. ADR이
  예측한 "S3에서만 드러나는 버그"의 실제 사례 — `S3_ENDPOINT_URL`이 있으면 path-style을 적용해 정정.
- **dev/CI = MinIO 자동 검증**: `tests/integration/test_storage_minio.py`가 presign 발급 → 실제
  업로드 → head → promote(copy+delete)와 put/get/delete를 MinIO에 태운다. `S3_ENDPOINT_URL` 없으면
  skip(로컬), CI는 공식 minio 컨테이너 + 버킷 생성으로 실행. presigned 주 업로드 경로가 비로소 자동
  검증된다.
- **direct multipart 제거(경로 ② 일원화)**: FE가 presigned 3단만 사용하게 된 뒤에도 direct
  업로드(`POST /media/images`·`/media/images/signup`)가 인증/비인증 풀셋(멱등 의존성·
  content-length 가드·매직바이트 스니핑 포함)으로 병행 유지되고 있었다 — 호출자 없는 두 번째
  파이프라인이자, 최대 20MB를 앱 서버 메모리로 태우는 경로. 전부 제거해 presigned 단일 경로로
  일원화하고, 비인증 signup 업로드 rate limit(IP당)은 살아남는 경로인 `signup/presign`·
  `signup/confirm`으로 이전했다(두 단계가 하나의 카운터를 공유 — 업로드 1건 = 2카운트).
- **pending/ 잔존물 GC = S3 lifecycle(infra 요건)**: confirm되지 않은 `pending/` 객체는 DB 행이
  없어 앱 sweeper가 지울 수 없다(고아 이미지 sweeper는 DB 행 기준). 앱이 S3 목록 순회 GC 잡을
  도는 대신 **`pending/` prefix에 lifecycle 만료 1일**을 버킷 요건으로 둔다 — presign TTL 15분·
  업로드 직후 confirm이라는 계약상, 1일 넘게 pending에 머무는 객체는 전부 미완료 잔존물이다.
  적용 지점: prod는 infra 저장소의 S3 버킷 정의(Terraform lifecycle rule), dev는
  `docker-compose.yml`의 minio-init가 `mc ilm rule add --expire-days 1 --prefix
  media/pending/`로 배선. 앱 코드는 관여하지 않는다(저장소 수명주기는 저장소 계층 책임 —
  목록 순회 잡은 봉투 대비 과잉).
- **`media/` 고아 회수 — 불변식으로 해소(backlog #44)**: 이전에는 "행이 존재한 적 없는 경로"가
  남아 있었다 — 승격 후 검증 실패나 confirm DB 실패 시 보상 삭제(`storage_delete`)마저 실패하면
  `media/` 아래에 아무도 못 찾는 객체가 남았다. `pending/`과 달리 `media/`는 "오래되면 잔존물"
  이라는 판별식이 없어 단순 만료도 못 건다. 목록 기반 GC를 infra 잡으로 두는 안을 검토했으나,
  **쓰기 순서 불변식으로 없애는 편이 싸고 확실하다** —
  [ADR 0019](0019-storage-db-write-ordering.md)에 결정과 대가를 기록했다.
