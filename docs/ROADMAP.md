# ROADMAP — 재건 진행 트래커 (living)

> 세션 인수인계·진행 추적용. 상세 근거는 각 **커밋 메시지**와 [`adr/`](adr/)에.
> 전제·범위는 [`00`](00-operating-envelope-and-scope.md), 횡단 결정은 [`01`](01-architecture.md).

## 단계
- [x] **Inception** — 운영 봉투·범위 (`00`)
- [x] **Elaboration** — 횡단 결정 (`01`) + ADR 0001~0006 + `/adr` 커맨드
- [x] **Construction** — 도메인 재건 (아래) **완료**
- [x] **Transition** — 관측성·스토리지·Docker/CI·모니터링 **완료**

## Construction 체크리스트 (재건 순서)

- [x] **기반층** — 설정 pydantic-settings + 프로덕션 가드 · 식별자 레거시 제거 · 구조화 로그(JSON/console)
- [x] **auth / users** — 감사 결과 P0/P1은 이전 하드닝으로 기적용, 마무리만
  - [x] #3 TOCTOU · #8 정지 토큰 무효화 · #9 bcrypt — 기적용 확인
  - [x] #7 인증 캐싱 — status 캐시 fast-fail 확정(ACTIVE는 PK+JOIN 유지; ADR 0004 근거), `auth.py:128` 타이핑 해소
  - [x] 테스트 보강 — #9 단위 · #8 통합
  - [x] 마감: `/security-review`(취약점 0) · `/code-review`(회귀 1건 발견→수정: `.env` 전용 `VIEW_CACHE_TTL_SECONDS`를 Settings 필드로 승격)
- [x] **media** — #1 고아 방지·업로드 멱등성 기적용 확인
  - [x] signup/orphan 정리를 트랜잭션 밖 스토리지 I/O + 배치로 정렬(형제 sweep와 통일)
  - [x] 리뷰 지적 수정: 정리 배치를 keyset(id>last_id) 전진으로 → 실패 머리 starvation·중복 로그 제거
  - [x] ADR 0008(POST 멱등성) 작성 · media 테스트 보강(멱등성 재생·409·정리 고아 방지·keyset 전진)
  - [x] 스토리지 전략 확정 — ADR 0010(S3 API 단일 경로·dev MinIO 패리티·local 폐기, 배선은 Ops)
  - [x] 마감: `/security-review`(취약점 0) · `/code-review`(정리 5건 → 헬퍼 추출·dead code 제거·설정 개명)
- [x] **posts** (핵심) — #2 CAS·#4 ILIKE 이스케이프·#17 해시태그는 기적용 확인(#17은 `{v}`가 load-bearing)
  - [x] #11 대표견만 로드 + 중복 eager-load 헬퍼 통합 · #12 해시태그 동기화 5→4왕복(upsert→조회)
  - [x] #10 커서 목록 `total` 제거 — `CursorPage` 분리(ADR 0002 결정을 코드로 구현)
  - [x] 조회수 write-behind 버퍼링 단위 테스트 보강(dedup·flush delta·CAS·재병합) + ADR 0007 작성
  - [x] 마감: `/code-review`(정확성 1건 → flush 커밋후 drain 삭제 실패 이중집계 수정) · `/security-review`(취약점 0)
  - [x] #11 심화(전용 `representative_dog` 뷰 관계로 `author.dogs` 부분 컬렉션 트랩 제거)는 **dogs 도메인에서 완료**
- [x] **comments / likes** — #11 twin 대표견만 로드 · #6 트리 페이지네이션 · #15 좋아요 카운터 중복
  - [x] #11 twin: 댓글 작성자 대표견만 로드(`_comment_author_loads`, posts와 동형)
  - [x] #6: 루트 keyset + 대댓글 부모별 배치 로드 + `CursorPage`(ADR 0002 결정을 코드로). 인메모리 슬라이스·500 cap·부정확 total 제거. 좋아요 keyset 드리프트 부정당 → 인기순(popular) 정렬 제거
    - 이후 인기순이 제품 요구로 복원됐다 — 루트 목록만 offset+`total`로 전환([ADR 0016](adr/0016-comment-list-offset-pagination.md)), 대댓글은 커서 유지. "변동 축에 keyset을 얹지 않는다"는 판단은 그대로다
  - [x] #15: 좋아요 카운터를 `CommentsModel`로 일원화(`CommentLikesModel` 중복 제거, posts 패턴 정합)
  - [x] 별건: 게시글 목록 `is_liked` 항상 False 버그 수정(배치 조회)
  - [x] 테스트: 트리 조립 단위(`test_comment_tree`) + keyset·삭제 시맨틱·is_liked·무이중집계 통합(`test_comments`·`test_posts`)
  - [x] 마감: `/code-review`(정리 3건 → 가시성 술어 공용화·중복 정렬 제거·is_liked 관용구 통일) · `/security-review`(취약점 0)
  - [~] 대댓글 자체 페이지네이션(루트당 preview + 더보기)은 기능 확장이라 backlog #21로 이연
- [x] **dogs** — #11 대표견 로딩 정리(전용 `representative_dog` 관계로 모델째 정리)
  - [x] #11 심화: 대표견을 `dogs`와 분리된 전용 `representative_dog` 뷰 관계(viewonly·uselist=False)로 로드해 부분 컬렉션 트랩(dogs 필터 로드가 컬렉션을 truncate) 제거. 프로퍼티→관계 단일 출처화, posts·comments 로더 전환
  - [x] 단일 대표견 불변식을 부분 유니크 인덱스(`owner_id WHERE is_representative`)로 DB 승격 + 마이그레이션(dedup 후 생성). upsert 대표 배정을 `set_representative`로 정규화(인덱스 안전). 근거 [ADR 0011](adr/0011-representative-dog-view-relationship.md)
  - [x] 테스트: 관계·인덱스 DDL·트랩 회귀 단위 + 프로필 dogs·대표견 공존·전환·인덱스 거부 통합
  - [x] 마감: `/code-review`(정확성 0 · 정리 1건 → 미사용 단일-행 CRUD 제거) · `/security-review`(취약점 0)
- [x] **chat / notifications** — #16 미읽음 스캔 · #19 방 중복조회 · 실시간(ADR 0009)
  - [x] #16: `list_recent_rooms`의 `unread`·`last_msg` 서브쿼리를 `room_id IN (내 방)`으로 스코프(전역 테이블 스캔 제거) + 미읽음 부분 인덱스(`ix_chat_messages_unread ... WHERE is_read IS false`). 마이그레이션 009
  - [x] #19: `get_room_peer_info` 방 이중 조회를 멤버십 접은 단일 쿼리로 병합. `list_room_messages`·`mark_room_read` 가드는 403 시맨틱 유지하며 2컬럼만 로드. `_is_room_member` 인라인화 제거
  - [x] 별건(감사): `notifications` 목록을 offset+`count(*)` → comments 동형 id keyset `CursorPage`로 정합화(ADR 0002). 인덱스 드리프트(004 `created_at` ↔ ORM) 해소, `(user_id, id DESC)`+미읽음 부분 인덱스. 마이그레이션 010
  - [x] **ADR 0009 실시간 전달** — 기구현된 WebSocket(chat)·SSE(notifications)×Redis Pub/Sub·fail-open·Celery 오프로드 설계를 소급 근거화
  - [x] 테스트: chat 미읽음 집계·멤버십 가드, notifications keyset·total 부재·전체읽음(무커버리지 해소)
  - [x] 마감: `/code-review`(정확성 2건 → 부분 인덱스 술어 정합·id keyset 전환) · `/security-review`(취약점 0)
- [x] **reports / admin** — #5 신고 목록 페이지네이션
  - [x] #5: 신고된 게시글·댓글을 DB-side `UNION ALL`로 합쳐 `report_count DESC, created_at DESC, id DESC` 단일 정렬·`LIMIT/OFFSET` + `count(*) over union`로 페이지·total을 DB 산출(`AdminReportsModel.page_reported_targets`). 페이지 `(type, id)`만 id 배치 하이드레이션해 UNION 순서 유지 — 인메모리 병합·500 cap·정렬 축 불일치 제거. 관리자 단독 offset 로더를 by-ids 로더로 대체(불필요 eager-load 축소)
  - [x] **ADR 0012 페이지네이션 전략** — offset+total을 저트래픽 admin 전제로 유지(공개 피드=cursor와 의도적 비대칭). ADR 0002의 인메모리 슬라이스 지적은 이행하되 메커니즘만 분기
  - [x] 부수: `reports(target_type, target_id) WHERE deleted_at IS NULL` 부분 인덱스(마이그레이션 011)로 집계 스캔 제거, 저자 없는(SET NULL) 신고 콘텐츠를 total·목록에서 일치 제외
  - [x] 테스트: UNION 페이지 컴파일·인덱스 존재·offset 로더 대체 단위 + 신고 피드 interleave·페이지 경계 무중복 통합(무커버리지 해소)
  - [x] 마감: `/code-review`(정확성 0·정리 0) · `/security-review`(취약점 0)
- [x] **정리(글로벌)** — #13 UserBlock 중복 인덱스 · #18 `_PG_UUID` 중복 · #20 `__future__` 일관성
  - [x] #13: `UserBlock`의 복합 PK와 중복인 `UniqueConstraint` 제거(형제 `PostLike`·`CommentLike`와 동형). 마이그레이션 `012`(head `011`에서 체인). block_user는 plain INSERT라 제약 참조 upsert 없음
  - [x] #18: 7개 모델 파일에 복제된 `_PG_UUID`를 `base_class.PG_UUID` 하나로 중앙화 — `as_uuid=True` 불변식 단일화(타입 인스턴스 공유는 SQLAlchemy에서 안전)
  - [x] #20: 35개 파일에만 있던 `from __future__ import annotations`를 **제거로 통일**(88개 이미 부재·`py311`·`TYPE_CHECKING` 없음). 드러난 미따옴표 forward-ref는 따옴표로 명시, 전 모듈 import 스모크로 NameError 부재 확인
  - [x] 마감: `/code-review`(정확성 0·정리 0 — 순수 정리라 복잡도 미추가) · ADR 불필요(load-bearing 결정 아님)

## Transition (Ops)
- [x] 관측성 인프라 — `/metrics`(prometheus) · 헬스 liveness/readiness 분리 ([ADR 0006](adr/0006-observability.md) 구현)
  - [x] 헬스 분리: `/livez`(의존성 무관 liveness) + `/readyz`(DB=hard→503, Redis=soft→report만). `/v1/health`는 ALB 하위호환 유지
  - [x] `/metrics`: `prometheus-client`로 RED(`http_requests_total`·`http_request_duration_seconds`·`http_requests_in_progress`). 라벨 `path`는 라우트 템플릿으로 카디널리티 제한, probe·`/metrics` 기록 제외
  - [x] ADR 0006 구현 노트로 구체화(Redis readiness=soft), 단위 테스트(외부 PG/Redis 불필요) 보강
- [x] 스토리지 — S3 단일 경로 + dev/CI MinIO 파리티 ([ADR 0010](adr/0010-storage-backend-strategy.md) 구현)
  - [x] local 디스크 백엔드·`STORAGE_BACKEND` 분기·`/upload` mount 제거. prod 검증에 S3 자격 필수 추가
  - [x] MinIO 호환: `S3_ENDPOINT_URL` 있으면 boto3 path-style — virtual-hosted DNS 미해석으로 깨지던 S3 전용 버그 정정(ADR이 예측한 사례)
  - [x] `test_storage_minio`가 presign→업로드→promote·put/get/delete를 MinIO에 태워 검증(로컬 skip, CI는 공식 minio 컨테이너+버킷). CI test 잡에 S3_* env 배선
- [x] 배포 — Docker · CI/CD (ECS는 infra repo에서 maintain+document)
  - [x] 멀티스테이지 `Dockerfile`(uv `--frozen --no-dev --no-install-project`·비루트·HEALTHCHECK `/livez`·gunicorn+uvicorn worker) + `.dockerignore`. 로컬 build·app 임포트·비루트 검증
  - [x] GitHub Actions: quality(poe lint/format/type/vulture) · test(postgres:15 서비스, unit+integration) · security(pip-audit informational) · docker(quality·test 통과 시 build, main push면 GHCR push)
  - [x] CI green화: celery pyright 오탐 한정 ignore, 미포맷 통합 테스트 포맷
- [x] 모니터링 · 로그 수집 ([ADR 0006](adr/0006-observability.md) 완결)
  - [x] 로그: 구조화 JSON(prod)·console(dev)·`request_id` 상관이 이미 구현(`logging_config.py`) — 수집(stdout→CloudWatch)은 ECS awslogs(infra) 몫
  - [x] 도메인 메트릭: `rate_limit_rejections_total{limit}`·`cache_events_total{cache,result}`·`view_buffer_flushed_views_total` 추가(default registry로 `/metrics` 노출). 운영 봉투 가정을 실측

## 2차 감사 (2026-07-13) — backlog #22~#36

전면 재감사 산출물. 항목 상세는 [`backlog.md`](backlog.md) 2차 감사 섹션. 권장 순서대로 진행:

- [x] **① #22** Celery 실배선 — SNS 배송을 `deliver_notification_sns`로 오프로드(재시도·백오프),
      dispatch 합성 API·미배선 태스크 제거, ENABLED=false/브로커 장애 인라인 폴백, 성공 후 멱등 마킹.
      단위 테스트 7종(enqueue·폴백·멱등 순서). ADR 0009 갱신
- [x] **② 핫패스·정확성**
  - [x] #27 미들웨어 순서 — RateLimit 최안쪽 재배치(429가 CORS·메트릭·로그 통과) + 심화:
        redis 탐색이 항상 None이던 결함을 `scope["app"]` 기반으로 교체해 **분산 rate limit 복원**
  - [x] #28 XFF 신뢰 — 원시 헤더 파싱 제거, 검증된 scope["client"]만 사용(조회수 조작 벡터 차단)
  - [x] #29 record_post_view 경량화 — EXISTS 가시성 확인 + reader/writer 세션 분리(폴백만 writer)
  - [x] 마감: `/code-review`(①+② 범위, 정확성 2건 반영 — ① 프로덕션 가드에 프록시 신뢰
        필수화: 복원된 IP rate limit이 ALB 뒤에서 프록시 IP로 수렴하는 자기-DoS 차단,
        ② Celery enqueue 소켓 타임아웃·publish 재시도 정책 명시. 정리 4건은 #25·#34·#35로 이연)
- [x] **③ 실시간 견고화** (마감 3차 + /security-review 신규 취약점 0)
  - [x] #23 SSE 팬아웃 통일 — `app/infra/pubsub.py` 공용 리스너(전용 연결 1개, chat·notif 채널
        동시 구독) + `SseFanoutManager` 로컬 큐. SSE의 공유 풀 pubsub 점유 제거, publish 실패 시
        로컬 폴백(chat 도달불가 폴백도 복원). ADR 0009 갱신
  - [x] #30 pubsub 재연결 — 공용 리스너에 지수 백오프(0.5s→30s, 구독 성공 시 리셋) 재연결.
        수신 계층 예외도 죽은 연결 무한 재시도 대신 재연결로 탈출, stop_event는 백오프 중 즉시 종료
  - [x] #32 WS 남용 방어 — 수신 루프에 유저 단위 fixed-window(`check_fixed_window` 공개 헬퍼:
        Redis 우선·장애 시 메모리 폴백) + `send_dm_from_ws`에 방향 무관 차단 검사(방 생성 전 거부)
  - [x] 마감 1차(인라인): 정확성 2건 반영 — WS 한도를 parse 앞으로 이동(잘못된 프레임 스팸도
        한도에 포함), WS 거부를 RATE_LIMIT_REJECTIONS에 집계. 보안 관점 신규 취약점 0
  - [x] 마감 2차(에이전트 /code-review, 견고성 5건 + 정리 반영):
        ① 차단 검사를 get_or_create_room 깊이로 이동 — REST 방 열기(`GET /chat/rooms/direct/…`)의
        우회 경로 차단 ② 로컬 전달을 publish 결과와 분리(로컬 우선 + envelope origin으로 자기
        발행분 스킵) — 리스너 재연결 창에서 같은 인스턴스 수신자 유실 제거 ③ 매니저 send 5s
        타임아웃 — 죽은 소켓 1개가 공용 리스너(인스턴스 실시간 전체)를 정지시키는 head-of-line
        차단 상한 ④ WS 한도 초과 시 억제 창(Redis 왕복 생략)+연속 30회 거부 시 1008 종료 —
        스팸의 Redis 부하 증폭 차단 ⑤ 백오프 리셋을 구독 성공→첫 폴 성공으로 — 플래핑 시 0.5s
        고정 재연결 루프 방지. 정리: rate limit 정책을 check_fixed_window 단일화(fail_open 플래그,
        메트릭 단일 집계점), WS 에러 프레임 헬퍼화, envelope 죽은 방어 분기 제거.
        이연: envelope 수신자 목록 단일화·block EXISTS 통합(#34), chat/pubsub 위임 모듈 정리(#35).
        ※ 파인더 4/8 완주(재사용·단순화·효율·설계심도), 검증은 인라인
  - [x] 마감 3차(중단됐던 정확성 3앵글+컨벤션 재실행, 견고성 6건 + 문서 4건 반영):
        ① 리스너 기동을 부팅 핑(app.state.redis)에서 분리 — 배포 중 Redis 순단이 크로스 인스턴스
        전달을 프로세스 수명 내내 죽이던 결함 ② envelope origin을 pid 기준 지연 생성 —
        gunicorn --preload fork 시 전 워커가 같은 ID를 물려받아 형제 워커 전달이 전멸하는 결함
        ③ 매니저 타임아웃 소켓을 실제로 close(1011) — 등록만 지우면 클라이언트가 수신만 조용히
        잃음 ④ 연속 거부 임계를 억제 창 밖(페이싱 우회)에도 적용 + 억제 창 거부도
        RATE_LIMIT_REJECTIONS 계측(count_rejection 단일 창구) ⑤ 백오프 리셋을 '5s 생존'으로
        (1s 폴 1회 성공으로 리셋되던 구멍) ⑥ 에러 프레임 전송 실패(RuntimeError)가 ASGI 예외로
        새던 것 억제. 문서: ADR 0009(로컬 우선+origin·send 타임아웃·백오프 근거), ADR 0003
        (WS 4번째 한도 클래스), README 503 잔재, chat/pubsub docstring. 이연: 큐 기반 소켓별
        전달·유저당 WS 연결 상한(#37 신설), 차단 시맨틱 비대칭(#36), FE openapi 재생성(#24)
- [x] **④ 미디어 정리** (마감 + /security-review 신규 취약점 0)
  - [x] #24 업로드 단일화 — FE가 presigned 3단만 호출함을 확인하고 direct multipart 2벌
        (엔드포인트 2·멱등 의존성 6·content-length 가드·매직바이트 스니핑·`storage_save`·
        관련 설정/예외) 전부 제거. signup IP 한도를 `signup/presign`·`signup/confirm`으로
        이전(공유 카운터, 기본 10→20 = 2카운트/업로드 보정). ADR 0008 범위 축소·0010 갱신.
        잔여: FE 죽은 경로 참조·openapi 타입 재생성(FE 작업)
  - [x] #31 presign 한도·pending GC — 한도는 #24와 함께 이전, pending/ GC는 S3 lifecycle
        만료 1일을 infra 요건으로 확정(ADR 0010, 앱 목록 순회 GC는 과잉이라 배제)
  - [x] 마감(에이전트 /code-review 8앵글 + 검증 2배치, CONFIRMED 15·PLAUSIBLE 3·REFUTED 0):
        ① confirm 검증(size·type)을 promote 앞으로 — 승격 후 거부가 남기던 DB 행 없는 영구
        객체 누수 제거(sweeper·lifecycle 둘 다 못 지우는 유일 경로), TOCTOU 재확인 실패 시
        승격본 보상 삭제 ② head 404를 400으로 매핑 — 미업로드/소진 키 confirm이 botocore
        ClientError로 500 나던 결함 ③ 인증 presign에 유저 단위 한도(`media_presign:{user_id}`
        100/시간, 다섯 번째 한도 클래스) — 일회용 계정으로 signup 한도를 우회하는 300배
        비대칭 봉합, `TooManyRequestsException`(429) 신설 ④ dev MinIO에 `mc ilm` pending
        만료 규칙 배선(문서로만 있던 GC 불변식 실체화) ⑤ 로컬 .env의 구 한도값(10) 갱신 —
        2카운트 의미론에서 5건/시간으로 반토막 나던 조용한 드리프트 ⑥ 라우터 주석 PUT→
        presigned POST 교정 ⑦ 도달 불가 검증(validate_purpose 트리오)·죽은 413 매핑 제거,
        낡은 docstring 정리. 문서: ADR 0003 결정 5항(공유 카운터 트레이드오프·안 한 선택
        명시), ADR 0008 '자연 멱등' 서술 정밀화(응답 재생 없음 트레이드오프), ADR 0010
        non-goal(바이트 검증 안 함 근거), 인덱스 2곳 0008 범위 교정. 이연: 비원자 promote
        동시성·트레일링 슬래시 이중 카운트(#34), 멱등 코어 단순화·경로 리터럴 드리프트·
        테스트 그림자 헬퍼(#35)
- [x] **⑤ 마무리** (마감 /code-review 완료 — 8앵글 파인더+검증, 정확성 5·정리 5건 반영:
      psycopg v3 sqlstate 교정·envelope 롤링 양방향 호환·view TTL 템플릿·/v1/health 한도
      복원·SNS 셧다운 드레인·deliver_once 단일화 등. 기각·이연 근거는 backlog #34~#36 노트)
  - [x] #25 조회수 경로 단일화 — FE 미사용 `POST /view` 제거, dedup→버퍼→폴백 안무를
        `_apply_view_increment` 헬퍼로 통일, 테스트를 GET 증가 경로로 이전·확장
  - [x] #33 트렌딩 timeout — 락 대기 타임아웃을 빈 목록 대신 loader(DB) 폴백으로,
        `on_wait_timeout` 제거(ADR 0004 정정 기록)
  - [x] #34 소품 — SNS 배송 `infra/sns.py` 통일(인라인 폴백도 멱등 스토어 공유·이중 배송 창
        봉합)·워커 Redis 재사용·`_SKIP_PATHS` /v1/health 교정·view TTL 0=dedup 끔 존중·
        `STORAGE_BACKEND` 잔재 제거·envelope 수신자 목록 1건 발행. 수용 3건은 backlog 기록 유지
  - [x] #35 죽은 코드 — MySQL 잔재→pgcode 매핑·미호출 메서드·도달 불가 분기 제거, auth 헬퍼
        공개 승격, redis 접근자 `get_app_redis` 단일화, 멱등 코어 post:create 인라인(-56줄),
        경로 리터럴 상수화+드리프트 가드, 테스트 페이크 공용화(`tests/unit/fakes.py`)+풀 스위트
        순서 의존 해소. 보류 2건은 backlog 기록 유지
  - [x] #26 ORM 배치 — `DogProfile`→dogs·`Report`→reports model.py 복귀(테이블 불변,
        문자열 관계+TYPE_CHECKING으로 순환 차단, configure_mappers 검증)
  - [x] #36 결정 반영·문서화 — 트렌딩 `window_hours` 제거·24h 고정(ADR 0004), 단일 세션·
        WS 토큰 쿼리스트링·차단 비대칭은 ADR 0013 신설로 확정

## 3차 감사 — backlog #37~#43

- [x] **#38** 댓글 블라인드 ↔ 게시글 `comment_count` 정합 — 조건부 UPDATE + RETURNING
      (`blind_if_visible`·`unblind_if_blinded`)으로 전이를 원자화(중복 블라인드가 카운트를 깎지 않는다)
- [x] **#39** 트렌딩 해시태그 집계 창 — `window_hours`(기본 24)·`name ASC` tie-breaker·희소 시 전체 기간 1회 폴백
- [x] **#40** 주기 정리 잡 분산 락 — `PeriodicJob`에 `lock_ttl_seconds`, `run_jobs_once`가 `lock:job:{name}`으로
      배타 실행. 요청 유발 sweep과 같은 키를 공유해 서로를 배제
- [x] **#41** 인덱스 마이그레이션 — 라이브 테이블은 `postgresql_concurrently` ([ADR 0015](adr/0015-index-migration-concurrently.md))
- [x] **#42** 실시간 연결 상한 — `REALTIME_MAX_CONNECTIONS_PER_USER`(기본 5)를 SSE·WS가 공유
- [x] **#43** 차단 목록 페이지네이션 — `CursorPage[BlockedUserItem]`, 커서 축 `blocked_id`
- [x] 댓글 목록 기본 정렬 **인기순 복원** — 루트 목록을 offset + `total`로 전환
      ([ADR 0016](adr/0016-comment-list-offset-pagination.md), `480a2ba0`). [`01`](01-architecture.md) C2에 개정 노트 반영
- [x] 라이브 데모 배포 — 단일 인스턴스 compose + Caddy ([ADR 0017](adr/0017-demo-deployment-topology.md))
- [ ] **#37** 실시간 전달 심화 — 큐 기반 소켓별 전달 (P2 · 이연. 연결 상한은 #42로 선반영)

## 완료 유닛 (커밋)

> 아래 표는 Construction~Transition 초기까지의 기록이다. 이후(2차·3차 감사) 작업은 위 체크리스트가
> 단위별로 대체한다 — 커밋 해시는 `git log`로 추적한다.

| 단위 | 커밋 |
|------|------|
| 설정 pydantic-settings | `919d0cbd` |
| 식별자 레거시 제거 | `99e72306` |
| 구조화 로그 | `2dd828e9` |
| auth 캐시 타이핑 정리 | `77ebdeae` |
| auth bcrypt·정지 테스트 | `ca952cde` |
| media signup 정리 트랜잭션 위생 | `b2579690` |
| media 멱등성·정리 테스트 | `f7edcd1b` |
| ADR 0008 멱등성 | `ecaf12c7` |
| media 정리 keyset 전진(리뷰 수정) | `7fedfd9e` |
| ADR 0010 스토리지 전략 | `80fa9049` |
| 백로그 docs/backlog.md 편입 | `d63c6ead` |
| media 정리 헬퍼 추출·dead code·개명(리뷰 수정) | `2b77838d` |
| posts 대표견만 로드·eager-load 헬퍼(#11) | `710c8d78` |
| posts 해시태그 5→4왕복(#12) | `3c4482b6` |
| posts 커서 목록 total 제거·CursorPage(#10) | `51b19c30` |
| posts 조회수 버퍼링 단위 테스트 | `4fa970be` |
| ADR 0007 조회수 write-behind | `4d73d07e` |
| posts flush 커밋후 삭제실패 이중집계 수정(리뷰) | `26947d41` |
| comments 대표견만 로드(#11 twin) | `8b4827cb` |
| comments 트리 루트 keyset+대댓글 배치·CursorPage(#6) | `ca8816ed` |
| 좋아요 카운터 중복 제거(#15) | `5cd35f57` |
| posts 목록 is_liked 계산 | `ad8ac277` |
| comments 트리 테스트 보강 | `8d5d5208` |
| comments 가시성 술어 공용화(리뷰) | `c777ae6e` |
| comments/likes docs 반영 | `7b819295` |
| dogs 대표견 전용 뷰 관계·트랩 제거(#11) | `b2ba3000` |
| dogs 대표견 부분 유니크 인덱스·마이그레이션(#11) | `37f0b4db` |
| dogs 대표견 테스트 보강 | `a232b7e6` |
| dogs 미사용 단일-행 CRUD 제거(리뷰) | `d0a8bdbe` |
| chat 미읽음·최근메시지 스캔 방 스코프(#16) | `a3080e33` |
| chat get_room_peer_info 이중조회 병합(#19) | `065ab645` |
| notifications keyset CursorPage·인덱스(ADR 0002) | `97489a93` |
| ADR 0009 실시간 전달 근거화 | `b8052887` |
| chat/notifications 테스트 보강 | `f6661b31` |
| chat/notif 리뷰 반영(인덱스 술어·id keyset) | `d3a4a05e` |
| 신고 목록 DB-side UNION ALL 페이지네이션(#5) | `b357850c` |
| reports (target_type, target_id) 부분 인덱스 | `af448bba` |
| ADR 0012 관리자 신고 피드 페이지네이션 | `137d806b` |
| reports/admin 테스트 보강 | `9c25cf37` |
| user_blocks 중복 UNIQUE 제거·마이그레이션 012(#13) | `170a5817` |
| _PG_UUID base_class 공용 타입 중앙화(#18) | `e03876d5` |
| `__future__` annotations 제거로 통일(#20) | `818444a6` |
| health liveness/readiness probe 분리(/livez·/readyz) | `d9230b41` |
| Prometheus /metrics + RED 메트릭 미들웨어 | `31136258` |
| in-progress 게이지 라벨 정리(리뷰 수정) | `a435e44e` |
| 멀티스테이지 Dockerfile + .dockerignore | `97ded4f7` |
| CI quality 게이트 green화(pyright 오탐·포맷) | `fd059c43` |
| GitHub Actions CI(quality·test·security·docker→GHCR) | `02c7170e` |
| local 스토리지 백엔드 제거·S3 단일 경로·path-style(ADR 0010) | `00c3bccb` |
| MinIO 파리티 통합 테스트 + CI 배선(ADR 0010) | `44fcd7f0` |
| 공개 URL media/ 프리픽스 정정(리뷰 수정) | `b7b72663` |
| 도메인 메트릭(rate-limit·cache·view-flush, ADR 0006) | `ba5b2e4b` |

> 백로그 번호(#n)는 [`backlog.md`](backlog.md) 기준.
