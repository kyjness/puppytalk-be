# 리팩토링 백로그 (Refactoring Backlog)

> 도메인 재건(Construction)이 소비하는 작업 소스. 각 항목의 **#번호**를
> [`00`](00-operating-envelope-and-scope.md)·[`ROADMAP`](ROADMAP.md)·[`adr/`](adr/)·커밋이 참조한다.
> **진행 상태는 [ROADMAP](ROADMAP.md)** 에서 추적하고, 여기서는 *항목·근거·수정 방향*을 담는다.
> (최초 인벤토리: 2026-06-06, `main` 기준 코드 분석.)

---

## P0 — 버그 (데이터 정합성·보안)

### 1. `cleanup_expired_signup_images` — 스토리지 삭제 실패해도 DB 레코드 삭제됨

**파일**: `app/domain/media/service.py`

```python
for img in rows:
    try:
        await asyncio.to_thread(storage_delete, img.file_key)
    except Exception as e:
        failed_file_keys.append(img.file_key)
    await MediaModel.delete_image_record(img, db=db)  # ← 실패해도 항상 실행됨
```

`storage_delete`가 예외를 던져도 `delete_image_record`가 호출된다. S3/로컬에는 파일이 남고 DB 레코드는 사라져 영구적인 고아 파일이 생긴다. `failed_file_keys`를 반환하지만 재시도 메커니즘이 없어 사실상 데이터 소실이다.

**수정**: 스토리지 삭제 실패 시 `continue` 추가.

```python
for img in rows:
    try:
        await asyncio.to_thread(storage_delete, img.file_key)
    except Exception as e:
        logger.warning(...)
        failed_file_keys.append(img.file_key)
        continue  # DB 삭제 건너뜀
    await MediaModel.delete_image_record(img, db=db)
```

---

### 2. View Flush 분산 락 — CAS 없는 삭제

**파일**: `app/domain/posts/services/post_service.py`

```python
finally:
    if lock_acquired:
        await redis_client.delete(VIEW_FLUSH_LOCK_KEY)  # ← CAS 없음
```

락 TTL(`VIEW_FLUSH_LOCK_SECONDS`)이 만료된 후 다른 워커가 락을 획득했을 때, 첫 번째 워커의 `finally`가 새 워커의 락을 삭제한다. 결과적으로 두 워커가 동시에 flush를 실행해 `view_count`가 두 배 증가할 수 있다.

`media/service.py`의 `_release_job_lock`은 Lua CAS로 정확히 구현했으나 여기서만 빠져 있다.

**수정**: `_release_job_lock` 패턴 동일 적용 — `SET NX`로 `lock_value`를 저장하고 Lua CAS로 해제.

---

### 3. `AuthService.signup` — TOCTOU 경쟁 조건

**파일**: `app/domain/auth/service.py`

```python
if await UsersModel.email_exists(data.email, db=db):
    raise EmailAlreadyExistsException()
if await UsersModel.nickname_exists(data.nickname, db=db):
    raise NicknameAlreadyExistsException()
# ...
created = await UsersModel.create_user(...)  # ← 여기서 IntegrityError 발생 가능
```

두 체크 사이 또는 체크-생성 사이에 동일 이메일/닉네임으로 동시 요청이 들어오면 `db.flush()`에서 `IntegrityError(23505)`가 발생한다. 현재 이를 잡는 코드가 없어 500 에러로 터진다.

**수정**: `create_user` / `db.flush()`를 `try/except IntegrityError`로 감싸고 `pgcode == "23505"` 시 적절한 예외로 변환.

---

### 4. ILIKE 패턴에 `%`, `_` 이스케이프 미적용

**파일**: `app/domain/posts/repository.py`

```python
pattern = f"%{token}%"
Post.title.ilike(pattern)
```

사용자가 `50%` 또는 `_abc_`를 검색하면 SQLAlchemy가 파라미터화하지만 ILIKE 와일드카드 문자는 이스케이프되지 않는다. `50%`는 "50으로 시작하는 모든 것"으로 동작해 의도와 다른 검색 결과가 반환된다.

**수정**:

```python
token_esc = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
pattern = f"%{token_esc}%"
Post.title.ilike(pattern, escape="\\")
```

---

## P1 — 높은 심각도 (잘못된 동작, 데이터 노출 위험)

### 5. `AdminService.get_reported_posts` — 메모리 기반 페이지네이션 버그

**파일**: `app/domain/admin/service.py`

```python
fetch_size = min(500, max(size * 2, page * size))
posts, total_posts = await PostsModel.get_reported_posts(page=1, size=fetch_size, db=db)
# ...
total = total_posts + total_comments  # DB에서 정확한 값
items = merged[start : start + size]  # 메모리에서 500건 범위 내 슬라이스
```

`total`은 DB에서 정확히 반환되지만 `items`는 최대 500+500건만 메모리에 올린 뒤 자른다. 예) size=20, 신고된 게시글 300건: page 26부터(`start=500`) 신고 데이터가 없어지고 API는 `total=300`을 보고하면서 빈 items를 반환한다.

**수정**: DB 쪽에서 합산 정렬 쿼리를 구성하거나(UNION), 게시글·댓글 신고 엔드포인트를 분리.

> **수정 완료(reports/admin 도메인)**: 두 테이블을 DB-side `UNION ALL`로 합쳐 `report_count DESC, created_at DESC, id DESC` 단일 정렬·`LIMIT/OFFSET` + `count(*) over union`으로 페이지·total을 DB에서 산출(`AdminReportsModel.page_reported_targets`). 페이지의 `(type, id)`만 받아 id 배치로 하이드레이션해 UNION 순서 그대로 조립 — 인메모리 병합·500 cap·정렬 축 불일치 제거. offset+total은 저트래픽 admin 전제에서 의도적으로 유지([ADR 0012](adr/0012-admin-report-feed-pagination.md)). 부수: `reports(target_type, target_id) WHERE deleted_at IS NULL` 부분 인덱스(마이그레이션 011)로 집계 스캔 제거, 저자 없는(SET NULL) 신고 콘텐츠를 total·목록에서 일치 제외.

---

### 6. 댓글 트리 500건 하드 리밋 + 인메모리 페이지네이션

**파일**: `app/domain/comments/model.py`, `app/domain/comments/service.py`

```python
# model.py
if fetch_all_for_tree:
    stmt = stmt.limit(500)  # ← 초과 시 무음 소실

# service.py
roots = _build_comment_tree(comments, ...)
total_count = len(roots)   # DB 실제 수가 아니라 메모리 루트 수
result = roots[start:end]
```

인기 게시글에 댓글 500건 초과 시: 500건만 로드해 트리를 구성하므로 나머지는 무음으로 사라진다. `total_count`도 실제 DB 수가 아니라 잘린 후 루트 수다. 5페이지 이상에서 실제로 존재하는 댓글이 보이지 않을 수 있다.

---

### 7. `get_current_user` — 매 요청마다 DB 조회 (인증 미캐싱)

**파일**: `app/api/dependencies/auth.py`

```python
async with db.begin():
    user = await UsersModel.get_user_by_id(user_id, db=db)  # JOIN 포함, 매 요청 실행
```

모든 인증된 요청이 `users` 테이블(+ profile_image JOIN)을 조회한다. `refresh_tokens`에는 `user:status:{user_id}` 캐시(TTL 240초)가 있는데 `get_current_user`에는 적용하지 않았다. 고트래픽 시 `users` 테이블이 핫스팟이 된다.

---

### 8. `suspend_user` — 기존 Access Token 즉시 무효화 안 됨

**파일**: `app/domain/admin/service.py`

`suspend_user`는 `invalidate_user_status_cache`만 호출한다. 정지된 유저의 Access Token은 최대 30분(`ACCESS_TOKEN_EXPIRE_SECONDS=1800`)간 유효하다. `revoke_refresh_for_user`가 존재하지만 `suspend_user`에서 호출되지 않는다.

**수정**: `suspend_user` 내에서 `AuthService.revoke_refresh_for_user` 호출 추가.

---

### 9. bcrypt 이중 실행 — pepper 기본값이 빈 문자열일 때

**파일**: `app/core/security.py`

```python
async def verify_password_with_legacy_fallback(plain: str, hashed_password: str) -> bool:
    if await verify_password(password_with_pepper(plain), hashed_password):
        return True
    return await verify_password(plain, hashed_password)
```

`PASSWORD_PEPPER`가 비어 있으면(기본값 `""`) `password_with_pepper(plain)`은 `plain`과 동일하다. 즉 첫 번째 시도가 실패하면 동일한 값으로 두 번 bcrypt를 실행한다. bcrypt는 의도적으로 느리므로 로그인 레이턴시가 두 배가 된다.

**수정**: `PASSWORD_PEPPER`가 비어 있으면 폴백 없이 단일 검증.

```python
async def verify_password_with_legacy_fallback(plain: str, hashed_password: str) -> bool:
    if settings.PASSWORD_PEPPER:
        if await verify_password(password_with_pepper(plain), hashed_password):
            return True
    return await verify_password(plain, hashed_password)
```

---

## P2 — 중간 심각도 (성능, 코드 구조)

### 10. `get_posts_count` — 커서 페이지네이션과 `COUNT(*)` 비용

**파일**: `app/domain/posts/services/post_service.py`

```python
total = await PostsModel.get_posts_count(db=db, search_q=search_q, ...)
```

커서 기반 페이지네이션에서 `total`은 의미가 제한적이다. 검색어가 있으면 `COUNT(*)`에도 `pg_trgm` 검색 필터가 적용돼 비용이 높다. 요청마다 목록 쿼리 + COUNT 쿼리 두 번을 실행한다.

---

### 11. `get_all_posts` — Dog 관계 과잉 로드

**파일**: `app/domain/posts/repository.py`

```python
joinedload(Post.user).selectinload(User.dogs).joinedload(DogProfile.profile_image),
```

게시글 목록에서 작성자의 모든 강아지 프로필 + 이미지를 전체 로드한다. 대표견 1마리만 필요한데 소유자별 강아지 전체를 가져온다. `User.representative_dog` 프로퍼티가 있어도 이미 모든 강아지가 메모리에 올라온다.

> **심화(감사 중 발견)**: 단순 과잉 로드에 더해, `dogs.and_(is_representative)` 필터 로드는 `User.dogs` **컬렉션 자체를 대표견 1마리로 truncate**해 세션에 캐시하는 **부분 컬렉션 트랩**이 있다 — 전체 `dogs`를 기대하는 프로필 경로가 조용히 누락된 데이터를 본다.
>
> **수정 방향**: 대표견을 `dogs`와 분리된 전용 `representative_dog` 뷰 관계로 로드(트랩 소멸)하고, 소유자당 대표견 1마리 불변식을 부분 유니크 인덱스로 DB 승격. → posts(#11)·comments(#11 twin)·dogs 도메인에서 구현. 근거: [ADR 0011](adr/0011-representative-dog-view-relationship.md).

---

### 12. `sync_post_hashtags` — 5회 왕복 쿼리

**파일**: `app/domain/posts/repository.py`

게시글 생성/수정마다 다음 5번의 DB 왕복이 발생한다:
1. `DELETE post_hashtags WHERE post_id = ...`
2. `SELECT Hashtag WHERE name IN (...)`
3. `INSERT INTO hashtags ... ON CONFLICT DO NOTHING`
4. `SELECT Hashtag WHERE name IN (...)` (재조회)
5. `INSERT INTO post_hashtags ... ON CONFLICT DO NOTHING`

3번에서 `RETURNING`을 사용하면 4번 재조회를 제거할 수 있다.

---

### 13. `UserBlock` — 복합 PK + UniqueConstraint 중복

**파일**: `app/domain/users/model.py`

```python
class UserBlock(Base):
    __table_args__ = (
        UniqueConstraint("blocker_id", "blocked_id", name="uq_user_blocks_blocker_blocked"),
    )
    blocker_id: Mapped[UUID] = mapped_column(..., primary_key=True)
    blocked_id: Mapped[UUID] = mapped_column(..., primary_key=True)
```

복합 PK가 이미 유니크를 보장하므로 `UniqueConstraint`는 중복된 인덱스다. 불필요한 인덱스를 마이그레이션으로 제거.

> **수정 완료(정리 도메인)**: `UserBlock.__table_args__`의 중복 `UniqueConstraint` 제거(복합 PK가 유니크 보장, 형제 `PostLike`·`CommentLike`와 동형). 마이그레이션 `012`에서 `drop_constraint("uq_user_blocks_blocker_blocked")`(head `011`에서 체인). block_user는 plain INSERT라 이 제약을 참조하는 upsert 없음을 확인.

---

### 14. `Settings` — pydantic-settings 미사용

**파일**: `app/core/config.py`

`Settings`는 일반 Python 클래스다:
- 환경 변수 타입 불일치 시 `ValueError` 발생 (컨텍스트 없음)
- 테스트에서 환경 변수 모킹이 어려움 (클래스 변수가 임포트 시 확정됨)
- `validate_settings_for_environment`가 `JWT_SECRET_KEY`만 검증하고 `DB_PASSWORD`, `COOKIE_SECURE` 등은 미검증

프로덕션 환경 추가 검증 권장:
- `COOKIE_SECURE=true`
- `TRUSTED_HOSTS != ["*"]`
- `DB_PASSWORD` 비어있지 않음
- `CORS_ORIGINS`에 `localhost` 미포함

---

### 15. `CommentLikesModel`과 `CommentsModel` 메서드 중복

**파일**: `app/domain/comments/model.py`

`CommentsModel.increment_like_count` / `decrement_like_count`와 `CommentLikesModel.increment_like_count` / `decrement_like_count`가 동일한 쿼리로 중복 정의되어 있다. `LikeService`에서 `CommentLikesModel` 버전을 사용해 일관성도 없다.

---

### 16. Chat `list_recent_rooms` — 미읽음 카운트 전체 테이블 스캔

**파일**: `app/domain/chat/service.py`

```python
unread = (
    select(...)
    .where(
        ChatMessage.is_read.is_(False),
        ChatMessage.sender_id != user_id,
    )
    .group_by(ChatMessage.room_id)
    .subquery()
)
```

`WHERE ChatMessage.room_id IN (user's rooms)` 조건 없이 전체 `chat_messages` 테이블을 스캔해 GROUP BY한다. 메시지가 쌓일수록 성능이 저하된다.

> **수정 완료(chat 도메인)**: `unread`·`last_msg` 두 서브쿼리를 `room_id IN (내 방)` 세미조인으로 한정하고, 미읽음 부분 인덱스 `ix_chat_messages_unread(room_id) WHERE is_read IS false`를 추가(술어를 쿼리의 `.is_(False)`와 동형으로 맞춰 플래너 매칭 보장). 실시간 전달 설계는 [ADR 0009](adr/0009-realtime-delivery.md).

---

## P3 — 낮은 심각도 (코드 품질, 마이너)

### 17. `VIEW_BUFFER_KEY` 리터럴 `{v}` — Redis Cluster 해시 태그 미작동

**파일**: `app/domain/posts/services/post_service.py`

```python
VIEW_BUFFER_KEY = "views:{v}:buffer"
VIEW_FLUSH_LOCK_KEY = "views:{v}:flush:lock"
```

Redis Cluster 해시 슬롯 제어를 위한 `{}` 문법처럼 보이지만 Python 포맷 스트링이 아니라 리터럴이다. `view:post:{post_id}:viewer:{viewer_key}` 키들과 다른 슬롯에 배치된다. Redis Cluster 도입 시 문제가 된다.

---

### 18. `_PG_UUID` 중복 정의

`app/domain/users/model.py`, `app/domain/comments/model.py`, `app/domain/posts/model.py`에 각각 `_PG_UUID = PG_UUID(as_uuid=True)`가 별도로 정의된다. `app/db/base_class.py`에 한 번만 정의하고 임포트.

> **수정 완료(정리 도메인)**: 실제로는 7개 모델 파일(`users·comments·posts·notifications·media·likes·chat`)에 복제돼 있었다. `base_class.PG_UUID` 하나로 정의하고 전부 임포트로 교체 — `as_uuid=True` 불변식을 한 곳에서 보장(동일 타입 인스턴스 공유는 SQLAlchemy에서 안전).

---

### 19. `chat/service.py:get_room_peer_info` — 채팅방 중복 조회

```python
rres = await db.execute(select(ChatRoom)...)  # 1차 조회 (멤버 확인)
# ...
stmt = select(...).where(ChatRoom.id == room_id)  # 2차 조회 (데이터)
res = await db.execute(stmt)
```

같은 `room_id`로 두 번 쿼리한다. 멤버 권한 확인과 데이터 조회를 단일 쿼리로 합칠 수 있다.

> **수정 완료(chat 도메인)**: 멤버십을 projection `WHERE`에 접어넣고(`or_(user1==me, user2==me)`) `one_or_none()→None→403`으로 1쿼리화. `list_room_messages`·`mark_room_read`의 가드는 서로 다른 연산 앞의 authz 단계라 403 시맨틱상 유지하되 전체 엔티티 대신 두 컬럼만 로드하도록 좁힘. 별건: 감사 중 `notifications` 목록이 offset+`count(*)`로 ADR 0002를 벗어나 있어 comments와 동형 id keyset(CursorPage)으로 정합화하고 인덱스 드리프트(004↔ORM)를 해소.

---

### 20. `from __future__ import annotations` 불일치

일부 파일(`auth/service.py`, `notifications/service.py` 등)에는 있고 다른 파일(`users/model.py`, `comments/model.py` 등)에는 없다. Python 3.11+에서는 대부분 불필요하지만 일관성 부족이다.

> **수정 완료(정리 도메인)**: 35개 파일에만 있던 future import를 **제거로 통일**(88개가 이미 부재, `target-version=py311`, `TYPE_CHECKING` 사용처 없음). 제거로 드러난 미따옴표 forward-ref(`posts.model`의 `Mapped[list["Post"]]`·`Mapped[list["PostImage"]]`, `users.schema`의 self 반환 애노테이션)는 따옴표로 명시. 전 모듈 import 스모크로 NameError 부재 확인.

---

### 21. 댓글 대댓글 배치 로드에 상한 없음 (대댓글 페이지네이션 부재)

**파일**: `app/domain/comments/model.py` (`get_replies_for_roots`)

#6 수정으로 루트는 keyset 페이지네이션되지만, 한 페이지의 루트들에 달린 대댓글은 `parent_id IN (root_ids)` 배치 1쿼리로 **전부** 로드한다(부모별 상한 없음). 인기글의 한 루트에 대댓글이 수천 건 달리면 한 응답이 그만큼의 행 + 작성자 eager load를 끌어온다. 옛 코드는 오히려 500 cap으로 대댓글을 무음 절단했으므로 정확성은 개선됐지만, 운영 봉투(인기 스레드)에선 상한이 필요하다.

**수정**: 루트당 대댓글 preview(top-N) + 별도 "대댓글 더보기" keyset 엔드포인트로 분리. 기능 확장이라 #6과 별개 단위로 다룬다.

> **수정 완료**(3차 감사): `get_reply_previews_for_roots`가 윈도우 함수로 루트당 상위 N건만 남기고,
> 같은 파티션 스캔의 `count() OVER`로 총 개수까지 얻는다 — 쿼리는 여전히 1회, 행 수는
> `루트 수 × N`으로 상한이 잡힌다(별도 COUNT 쿼리 없음). 나머지는
> `GET /posts/{post_id}/comments/{comment_id}/replies`가 루트 목록과 **같은 keyset 패턴·같은
> 가시성 술어**로 이어 받아, 목록·preview·더보기의 규칙이 갈라지지 않는다. 대댓글 id나 남의
> 게시글 id 조합은 404 — 2단 트리 구조를 유지한다. `replies`가 preview가 되고
> `reply_count`·`has_more_replies`가 붙는 계약 변경이라 FE 동반 수정 대상.

---

## 2차 전면 감사 (2026-07-13) — #22~#36

> Construction/Transition 완료 후 전체 코드 재감사에서 나온 항목. 기준은 [`00`](00-operating-envelope-and-scope.md)의
> 운영 봉투와 "정당화된 복잡도" 원칙. 진행 순서는 [ROADMAP](ROADMAP.md) 2차 감사 섹션.

### 22. Celery 파이프라인이 실경로에 미배선 (장식화) — P1

**파일**: `app/core/celery.py`, `app/worker/*`, `app/domain/notifications/router.py`

프로덕션 코드에서 태스크를 enqueue하는 곳이 `POST /notifications/{id}/dispatch`(사용자가 자기 알림 재전달을 큐잉하는 합성 API) 하나뿐이다. 실제 알림 배송은 서비스에서 인라인 Redis publish + fire-and-forget SNS(`to_thread`)로 수행되어, ADR 0009의 "Celery 오프로드"와 실코드가 불일치한다. 현 상태는 스택 전체(celery.py·async_bridge·worker/·큐 라우팅·설정 13개)가 사실상 전시물이다.

**수정 방향**: (a) SNS publish·오프라인 배송을 Celery로 실배선하고 dispatch 엔드포인트 제거, 또는 (b) Celery 전체 제거 + "쓰기 초당 수십 규모에선 인라인 발행으로 충분"을 ADR로 기록. 채택안은 착수 시 결정.

> **수정 완료 — (a) 실배선 채택**: 알림 생성 시 `publish_after_commit`이 SNS 배송을
> `deliver_notification_sns`(high_priority)로 enqueue(비동기 컨텍스트 블록 방지 위해 `to_thread`,
> 결정적 멱등키 `sns:{notification_id}`). `CELERY_ENABLED=false`·브로커 장애는 기존 인라인
> fire-and-forget으로 폴백. 워커 잡은 DB 행에서 페이로드 재구성, SNS client 프로세스당 재사용,
> **publish 성공 후에만 멱등 마킹**(#34 첫 항목 선반영 — 선마킹이면 실패 재시도가 skip으로 유실).
> 합성 dispatch 엔드포인트·미배선 `mark_notifications_read_job`·구 재전달 잡 제거. ADR 0009 갱신.

---

### 23. SSE 알림 pubsub가 공유 풀 연결 점유 (풀 고갈 → 전면 fail-open) — P1

**파일**: `app/domain/notifications/service.py` (`sse_subscribe`)

SSE 연결마다 `app.state.redis` 공유 풀(128)에서 pubsub 연결을 점유한다. 동시 SSE가 풀 한도에 근접하면 rate limit·인증 상태 캐시·조회수 버퍼가 연결 고갈로 일제히 fail-open된다(수천 DAU 전제에서 현실적). chat은 동일 문제를 단일 채널 + 인스턴스당 전용 구독 1개 + 로컬 팬아웃으로 이미 풀었다 — 같은 문제를 두 패턴으로 유지 중.

**수정 방향**: 알림도 chat 동형으로 통일(인스턴스당 전용 연결 구독 1개 → 로컬 SSE 클라이언트 팬아웃). ADR 0009 갱신.

> **수정 완료**: fanout 공용 인프라 `app/infra/pubsub.py` 신설 — envelope publish(성공 여부 반환)
> + **전용 연결 1개로 chat·notif 채널 동시 구독** 리스너(`run_user_fanout_listener`). 알림은 단일
> 채널 `puppytalk:channel:notif:sse` + `SseFanoutManager`(유저별 bounded 큐 100, 가득 차면 드롭)로
> 전환 — `sse_subscribe`는 Redis 미점유 로컬 큐 대기. publish 실패·Redis 부재 시 로컬 직접 전달로
> 폴백하고 SSE 503 분기 제거(fail-open). 부수 수정: chat `_fanout_dm`의 로컬 폴백이 publish 내부
> 예외 삼킴 때문에 **도달 불가능**하던 결함을 성공 여부 반환으로 복원. ADR 0009 갱신, 테스트 12종.

---

### 24. 업로드 경로 2벌 공존 (direct multipart + presigned) — P2

**파일**: `app/domain/media/router.py`, `app/domain/media/image_policy.py`

ADR 0010이 "S3 단일 경로"를 선언했지만, direct 업로드(`/media/images`, `/media/images/signup`)와 presigned 3단(presign→S3 직접→confirm)이 인증/비인증 각각 풀셋으로 공존한다(엔드포인트 6개, 파이프라인 2벌). direct는 최대 20MB를 서버 메모리로 태운다.

**수정 방향**: presigned로 단일화하고 direct 제거(FE 전환 포함). direct를 유지한다면 사유를 ADR 0010에 추가.
FE 전환 시 openapi 생성 타입 재생성 포함 — FE `schema.d.ts`에 제거된 `NOTIFICATION_SSE_UNAVAILABLE` 등
BE에서 사라진 계약이 잔존 중(③ 마감 리뷰 발견).

> **수정 완료(BE)**: 검증 결과 FE는 이미 presigned 3단만 호출(direct는 런타임 사용 0건) —
> direct 2개 엔드포인트와 전용 파이프라인 전부 제거: `save_image_for_media`(매직바이트 스니핑·
> 청크 읽기 포함), 미디어 멱등 의존성 6개(`media:upload`·`media:signup` 네임스페이스),
> `check_upload_content_length`(모듈째), `MAX_FILE_SIZE`·`IDEMPOTENCY_MEDIA_UPLOAD_*` 설정,
> `FileSizeExceededException`·`PayloadTooLargeException`(413 코드는 전역 매핑용으로 유지),
> `storage_save`. 비인증 signup 업로드 IP 한도는 살아남는 `signup/presign`·`signup/confirm`으로
> 이전(공유 카운터, 기본값 10→20 = 업로드 1건당 2카운트 보정, #31 연계). ADR 0008 적용 범위
> 축소·ADR 0010 구현 노트·README 갱신. **잔여(FE)**: `client.ts` 죽은 경로 참조 정리 +
> openapi 타입 재생성 — FE 작업에서 처리.

---

### 25. 조회수 기록 경로 2벌 (GET 상세 자동 증가 + POST /view) — P2

**파일**: `app/domain/posts/routers/post_router.py`

`GET /posts/{id}`가 조회수를 자동 증가시키는데 `POST /posts/{id}/view`도 따로 있다. dedup 키가 이중 집계는 막지만 동일 기능 API가 둘.

**수정 방향**: FE 사용 경로 확인 후 한쪽 제거. **(①+② 마감 리뷰)** #29로 두 경로가 dedup→버퍼→writer 폴백 안무를 중복 구현하게 됐다 — 한쪽을 제거하면 자연 해소되고, 둘 다 남길 경우 공용 헬퍼로 추출.


> **수정 완료(posts 도메인)**: FE 실사용 확인 — GET 상세만 호출, `POST /view`는 호출처 0건
> (openapi 생성 타입에만 존재). POST 엔드포인트·`record_post_view` 제거, dedup→버퍼→writer 폴백
> 안무를 `_apply_view_increment` 공용 헬퍼로 추출해 `get_post_detail`이 사용. 테스트 자산은 전부
> POST 쪽에 있었으므로 무테스트였던 GET 증가 경로로 이전·확장(안무 계약·404·writer 폴백+응답
> 즉시 반영·버퍼 pending 반영). `post_is_visible`은 comments·likes가 사용해 유지.
---

### 26. ORM 클래스 소속 도메인 불일치 — P3

**파일**: `app/domain/users/model.py` 외

`Report`·`DogProfile` ORM이 `users/model.py`에 정의되어 있고 reports/·dogs/ 도메인엔 쿼리 클래스만 있다. 또 posts만 `repository.py + services/ + routers/` 분리이고 나머지 도메인은 `model.py`가 ORM+쿼리 이중 역할.

**수정 방향**: ORM 배치 규약을 하나로 통일(각 도메인 model.py 복귀 또는 `app/db/models/` 집결). 대규모 이동이라 순서 마지막.

---

### 27. 429 응답이 CORS·메트릭·접근로그 바깥에서 종료 — P1

**파일**: `app/main.py`(미들웨어 등록 순서), `app/core/middleware/rate_limit.py`

등록 순서상 RateLimit이 CORS/metrics/access_log보다 바깥 껍질이다. 브라우저 FE는 429를 CORS 에러로 수신해 `retry_after_seconds`를 읽지 못하고, 429가 RED 메트릭(`http_requests_total`)·접근로그에서 누락된다 — "rate limit 발동을 실측한다"(ADR 0006)와 모순.

**수정 방향**: RateLimit을 관측·CORS 미들웨어 안쪽으로 재배치(또는 `_send_429`에 CORS 헤더 부착). 순서 주석 갱신.

> **수정 완료 + 심화(수정 중 발견)**: 재배치 검증 중 `_get_app_with_state`가 미들웨어 체인을
> `.app`으로 순회하는 방식이 **항상 None을 반환**함을 런타임으로 확인 — 체인의 어떤 노드도
> `.state`를 갖지 않아 **Redis 분산 rate limit이 전 구간 조용히 비활성**이었다(전역 한도 무동작,
> 로그인·가입 한도는 인스턴스별 메모리 폴백만). 탐색을 Starlette 표준 `scope["app"]` 기반
> `_redis_from_scope`로 교체해 복원. 배치는 RateLimit을 최안쪽으로 이동 — 429가 CORS(브라우저가
> Retry-After를 읽음)·RED 메트릭(`path=__unmatched__`로 집계, 카디널리티 보호 유지)·접근로그·
> X-Request-ID를 거쳐 나간다. FakeRedis + TestClient로 두 불변식(스코프 탐색 동작·429 관측 통과)
> 회귀 고정.

---

### 28. `get_client_identifier`가 X-Forwarded-For 무검증 신뢰 (조회수 조작 벡터) — P0

**파일**: `app/api/dependencies/client.py`

`ProxyHeadersMiddleware`가 신뢰 프록시 검증 후 `scope["client"]`를 갱신하는데, 이 함수는 원시 XFF 헤더를 우선한다. 요청마다 위조 XFF를 넣으면 viewer_key가 매번 달라져 조회수(→트렌딩 랭킹)를 무한 부풀릴 수 있고, signup 업로드 멱등 스코프도 위조된다.

**수정 방향**: 검증 완료 값(`request.client.host`)만 사용.

> **수정 완료**: 원시 XFF 파싱 분기를 제거하고 `scope["client"]`(신뢰 프록시 뒤에서는
> ProxyHeadersMiddleware가 이미 실제 IP로 갱신)만 사용 — rate limit 키 산정과 동일 규약으로
> 통일. 위조 XFF 무시·프록시 갱신값 준수를 단위 테스트로 고정.

---

### 29. `record_post_view` 존재확인이 풀 eager-load + master 세션 — P1

**파일**: `app/domain/posts/services/post_service.py`, `post_router.py`

가장 뜨거운 쓰기 엔드포인트가 존재/가시성 확인용으로 상세 eager-load 4종(`get_post_by_id`)을 실행하고, 라우터가 master 세션을 준다. 조회 폭주 1순위 봉투와 정면 배치.

**수정 방향**: `post_is_visible`(EXISTS 1쿼리) + slave 세션으로 교체.

> **수정 완료**: 가시성 확인을 `post_is_visible`(EXISTS)로 교체하고 라우터를 reader+writer
> 이중 세션(get_post_detail과 동형)으로 전환 — 확인은 reader, Redis 버퍼 실패 폴백 increment만
> writer. 커버리지 0이던 경로에 단위 테스트 3종(풀 로드 금지·불가시 404·폴백은 writer로) 추가.

---

### 30. chat pubsub 리스너 재연결 부재 — P1

**파일**: `app/domain/chat/pubsub.py` (`run_chat_subscribe_listener`)

기동 시 `ping()` 실패나 루프 밖 예외 1회면 리스너 태스크가 조용히 종료되고, 해당 인스턴스의 크로스 인스턴스 DM 수신이 프로세스 재시작까지 죽는다(내부 `get_message` 예외만 재시도).

**수정 방향**: 백오프 재연결 루프로 감싸기 — 멀티 인스턴스 3~10대·99.9% 전제에서 정당한 복잡도.

> **수정 완료**: #23에서 통합된 공용 리스너(`app/infra/pubsub.py`)에 지수 백오프 재연결
> (0.5s→30s 캡, 구독 성공 시 리셋) 적용 — chat·notif 채널 동시 복구. 연결 1회분을
> `_listen_once`로 분리해 접속·수신 계층 예외는 재연결로 던지고(기존 "죽은 연결로
> get_message 무한 재시도" 결함 제거), 핸들러·envelope 오류는 삼켜 연결을 유지한다.
> stop_event는 백오프 대기 중에도 즉시 종료. 재연결 테스트 4종.

---

### 31. presign 남용 방어·pending/ 객체 GC 부재 — P1

**파일**: `app/core/middleware/rate_limit.py`, `app/infra/storage.py`, ADR 0010

signup 전용 한도(10/시간)가 `/media/images/signup` 정확 일치라 presign/confirm 경로엔 미적용(글로벌 100/분만). confirm되지 않은 `pending/` S3 객체는 DB 행이 없어 어떤 sweeper도 지우지 못한다 → 비로그인 IP당 분당 100회 presign×10MB 업로드가 영구 잔존 가능.

**수정 방향**: presign·confirm 경로를 signup 한도에 포함 + `pending/` prefix에 S3 lifecycle(예: 1일 만료)을 infra 요건으로 ADR 0010에 명시.

> **수정 완료**: 한도는 #24 direct 제거와 함께 이전 — `_path_is_signup_upload`가
> `signup/presign`·`signup/confirm`을 매칭(공유 카운터, 기본 20/시간 = 업로드 10건분,
> critical path라 Redis 장애 시 메모리 폴백 유지). pending/ GC는 **S3 lifecycle 만료 1일을
> infra 요건으로 ADR 0010 구현 노트에 명시**(prod Terraform lifecycle rule + dev/CI MinIO
> `mc ilm` 등가 규칙) — 앱 목록 순회 GC 잡은 봉투 대비 과잉이라 하지 않음. infra 저장소
> 작업 시 버킷 정의에 규칙 반영 필요.
>
> **④ 마감 리뷰 보강**: confirm 검증(size·content-type)을 promote **앞**으로 이동(승격 후
> 거부가 남기던 DB 행 없는 영구 객체 누수 제거, TOCTOU 재확인 실패 시 승격본 보상 삭제),
> head 404를 ValueError→400으로 매핑(미업로드/소진 키 confirm이 500으로 새던 결함),
> **인증 presign에 유저 단위 한도**(`media_presign:{user_id}` 100/시간 — 일회용 계정으로
> signup 한도를 우회하는 비대칭 봉합, ADR 0003 결정 5항), dev MinIO에 `mc ilm` 규칙 배선.

---

### 32. WebSocket DM에 rate limit·차단 검사 부재 — P1

**파일**: `app/core/middleware/rate_limit.py`(ws scope 통과), `app/domain/chat/service.py`

rate limit 미들웨어는 `scope["type"] != "http"`를 그대로 통과시켜 WS 접속 1회로 무제한 DB 쓰기+팬아웃이 가능하다. `send_dm_from_ws`는 UserBlock을 확인하지 않아 차단한 상대에게서 DM이 온다.

**수정 방향**: WS 수신 루프에 유저 단위 한도(Redis fixed-window 재사용) + 차단 관계 검사 추가.

> **수정 완료**: rate_limit 모듈에 공개 헬퍼 `check_fixed_window`(Redis Lua fixed-window 우선,
> 부재·장애 시 인스턴스 로컬 메모리 폴백 — 남용 방어라 완전 fail-open 안 함) 추출, WS 수신
> 루프에서 `chat:ws:{user_id}` 키로 검사(기본 60건/60초, 초과 시 `rate_limited` + retry_after
> 에러 프레임, 연결은 유지). `send_dm_from_ws`는 방 생성 전에 `block_exists_between`(방향 무관
> OR 술어 1쿼리)으로 거부 — 응답 문구는 차단 방향을 노출하지 않는 중립 표현. 테스트 6종.

---

### 33. 트렌딩 캐시 wait-timeout이 빈 목록 반환 — P2

**파일**: `app/infra/cache.py`, `trending_post_service.py` (`on_wait_timeout=[]`)

락 경합으로 2초 대기 후 타임아웃되면 사용자에게 빈 인기글이 내려간다. 가용성 우선이라 해도 "틀린 데이터"보다 loader(DB) 폴백이 봉투에 부합(대기자 수만큼의 쿼리는 감내 가능).

**수정 방향**: timeout 시 loader 폴백으로 변경, 또는 현행 유지 근거를 ADR 0004에 명시.


> **수정 완료(infra)**: `on_wait_timeout` 매개변수 제거 — 락 대기 타임아웃·대기 중 Redis 오류
> 모두 loader(DB) 폴백으로 통일(소비자 2곳 갱신). 빈 값은 "틀린 데이터"이고 대기자 수만큼의
> 쿼리는 봉투 내 감내 가능(ADR 0004에 정정 근거 기록). 락 경합+캐시 미충전 시 loader 호출
> 테스트 추가.
---

### 34. 소품 모음 (정확성·표기 드리프트) — P2

- ~~`worker/jobs/notification_delivery.py`: idempotency 키를 publish **전에** 선점~~ → **#22에서 선반영 완료**(성공 후 마킹).
- `notifications/service.py`: 인라인 SNS 폴백이 publish마다 boto3 client 신규 생성 + `create_task` 참조 미보관(GC 유실 가능). **(①+② 마감 리뷰 보강)** SNS publish 헬퍼가 서비스(인라인)·워커 잡에 2벌 존재 — 공용 cached-client 헬퍼 하나로 합치고 `_run` 래퍼·중복 `SNS_TOPIC_ARN` 가드도 함께 제거. 인라인 폴백 경로도 워커와 같은 `celery:notif:delivered:` 멱등 스토어를 확인하면 브로커 ack 유실 시 이중 배송 창이 닫힌다.
- **(①+② 마감 리뷰)** `worker/jobs/notification_delivery.py`: 태스크 실행마다 Redis 클라이언트 `from_url`→`aclose` — 워커 프로세스당 클라이언트 재사용으로 교체.
- `rate_limit.py` `_SKIP_PATHS`의 `"/health"`가 실경로 `/v1/health`와 불일치.
- `docker-compose.yml` `VIEW_CACHE_TTL_SECONDS: "0"` — dedup 끔 의도로 보이나 코드는 0→3600 폴백이라 로컬 조회수가 안 오름. 의도 정렬.
- `docker-compose.yml` 폐기 설정 `STORAGE_BACKEND` 잔재 제거.
- **(③ 마감 리뷰)** chat `_fanout_dm`이 같은 wire를 envelope 2건(peer·sender)으로 발행 — 전 인스턴스가
  중복 파싱. envelope에 수신자 목록(`target_user_ids`)을 담아 1건으로 합치면 발행 RTT·리스너 부하 절반.
- **(④ 마감 리뷰)** `storage._promote_pending_object_sync`의 head→copy→delete가 비원자 — 동시 confirm
  2건이 모두 성공해 객체·DB 행이 일시 중복될 수 있다(중복분은 DB 행이 있어 24h 후 orphan sweeper가
  수거). 발생 빈도·피해가 작아 수용 중 — 필요 시 pending 키 기준 Redis `SET NX` 짧은 락으로 직렬화.
- **(④ 마감 리뷰)** rate limit 경로 매칭이 트레일링 슬래시를 rstrip으로 수용 → `/presign/` 호출은
  Starlette 307 리다이렉트 전후로 2회 카운트(로그인에도 있던 기존 패턴, 2단계 업로드에서 영향 2배).
  정상 클라이언트는 슬래시 없이 호출하므로 수용 — 정밀화하려면 `redirect_slashes=False` 또는 미들웨어에서
  307 예상 경로 스킵.
- **(③ 마감 리뷰)** `send_dm_from_ws`의 차단 EXISTS가 peer 조회와 별도 왕복 — peer SELECT에 exists
  서브쿼리로 합치면 메시지당 DB 왕복 1회 절감(60/분 한도 하에서는 마이너).


> **수정 완료(수용 3건 제외)**: ① SNS publish를 `app/infra/sns.py`로 통일 — 프로세스 캐시
> 클라이언트 + 멱등 스토어(already/mark_delivered)를 서비스 인라인 폴백과 워커 잡이 공유해
> 브로커 ack 유실 교차 경로의 이중 배송 창 봉합, `create_task` 참조는 모듈 set+done-callback
> 보관, `_run` 래퍼·중복 가드·region 폴백 제거 ② 워커 Redis 클라이언트 태스크당
> from_url→aclose를 프로세스당 재사용으로 ③ `_SKIP_PATHS` "/health"→실경로(/v1/health) 교정
> (#35 경로 상수화 커밋에 흡수) ④ compose `VIEW_CACHE_TTL_SECONDS=0` 의도를 코드가 존중
> (0→3600 폴백 제거 = dedup 끔) ⑤ `STORAGE_BACKEND` 잔재 제거 ⑥ `_fanout_dm` envelope 2건 →
> 수신자 목록(`target_user_ids`) 1건(파서는 구포맷 스칼라 수용 — 롤링 배포 창).
> **수용 유지(기록대로)**: 비원자 promote·트레일링 슬래시 이중 카운트·차단 EXISTS 왕복 통합.
---

### 35. 죽은 코드 일괄 — P3

- `core/exception_handlers.py`: MySQL 잔재(errno 1062/1451/1452, `"key 'email'"` 메시지 파싱) — PostgreSQL 전용 스택에서 도달 불가. pgcode·constraint_name 기반으로 정리.
- `db/base_class.py`: `Base.update()`·`soft_delete()` 호출처 없음.
- `likes/service.py`: `except IntegrityError` 분기 — create가 `ON CONFLICT DO NOTHING`이라 도달 불가(도달 시 별도 커넥션까지 여는 무거운 처리).
- `comments/model.py`: `CommentsModel.get_liked_comment_ids_for_user` 단순 위임 잔재.
- `posts/services/post_service.py`: `_VIEW_REDIS_EX_SECONDS <= 0` 분기 도달 불가(폴백이 3600 보장).
- `api/dependencies/auth.py`: auth 서비스의 `_`프라이빗 헬퍼 3개 크로스 모듈 임포트 → 공용 모듈로 승격.
- **(①+② 마감 리뷰)** app.state.redis 접근자가 3벌(`get_optional_redis`는 isinstance 가드, `_redis_client`·`_redis_from_scope`·라우터 bare getattr는 무가드)로 드리프트 — 공용 접근자 하나로 통일.
- **(①+② 마감 리뷰)** 단위 테스트의 FakeRedis·FakeDB류가 4개 파일에 각자 구현 — conftest 공용 픽스처로 승격
  (③에서 test_sse_fanout·test_pubsub_reconnect·test_chat_ws_guards 3개 파일이 추가로 늘어남).
- **(③ 마감 리뷰)** chat `ConnectionManager`·notif `SseFanoutManager`가 "유저별 집합 + asyncio.Lock" 골격을
  중복 구현 — 전달 세맨틱(WS send vs 큐 put)이 달라 억지 통합은 비권장이나, 등록/해제 골격의 공용 베이스는 검토 가치.
  임계 구역에 await가 없어 락 자체가 불필요하다는 지적도 함께 판단(제거 시 두 매니저 일관되게).
- **(③ 마감 리뷰)** `chat/pubsub.py`가 채널 상수 + `publish_chat_dm` 1줄 위임만 남음 — 상수를 도메인 모듈로
  옮기고 `publish_user_envelope` 직접 호출로 모듈 제거 검토(위임 잔재 정리와 동일 패턴).
- **(④에서 발견)** 테스트 순서 의존 결함: `tests/integration/conftest.py`의 세션 fixture
  `relax_integration_rate_limits`가 전역 settings 한도를 영구 완화 → 풀 스위트(integration→unit 순)에서
  `tests/unit/test_domain_metrics.py::test_rate_limit_rejection_increments_counter`(기본 login 한도 5 전제)가
  실패한다. 단위 테스트가 한도를 명시적으로 monkeypatch하거나, 완화를 통합 스위트 스코프로 격리.
- **(④ 마감 리뷰)** `api/dependencies/client.py` 멱등 코어가 다중 네임스페이스용 매개변수 표면
  (namespace·scope_parts·cache_adapter·conflict_message·lock_ttl·success_status)을 유지하지만 소비자는
  `post:create` 하나 — 래퍼 계층을 코어에 접거나 상수를 인라인해 한 층으로 단순화(~60줄).
- **(④ 마감 리뷰)** rate limit의 경로 리터럴(`_path_is_login`·`_path_is_signup_upload`)이 라우터 소유
  경로 문자열의 사본 — 라우트 개명 시 전용 한도가 조용히 글로벌로 강등되고, 미들웨어가 라우팅 전에
  동작해 테스트도 드리프트를 못 잡는다. 공용 상수로 묶거나 라우트 dependency로 내리는 방안 검토.
- **(④ 마감 리뷰)** `test_storage_minio.py`의 `_put_object` 헬퍼가 삭제된 storage_save를 프라이빗
  내부로 재구현 — presigned 승격 테스트에 불변식 검증을 접고 헬퍼를 제거하는 통합 검토(독립성 유지가
  낫다는 반론도 있어 보류).


> **수정 완료(보류 2건 제외)**: MySQL 잔재 예외 파싱 → pgcode(23505/23503)+psycopg diag 제약명
> 기반 재작성(+매핑 테스트) · `Base.update/soft_delete` 제거 · likes `except IntegrityError`
> 2분기+`_is_unique_violation`+고아 `AlreadyLikedException` 제거 · 위임 잔재
> `get_liked_comment_ids_for_user` 제거 · `_VIEW_REDIS_EX_SECONDS <= 0` 분기는 "TTL 0 = dedup
> 끔"으로 도달 가능해져 해소(#34④) · auth 프라이빗 헬퍼 3개 → `auth/user_status_cache.py` 공개
> 모듈 + `infra/redis.bulk_to_str` 승격 · app.state.redis 접근자 3벌+bare getattr ~18곳 →
> `get_app_redis` 단일 창구 · FakeRedis/FakeDB류 → `tests/unit/fakes.py` 공용화(6파일 이전) ·
> chat/pubsub 위임 모듈 제거(#34⑥ 커밋에 흡수) · 통합 conftest 한도 완화의 풀 스위트 순서 의존
> → unit 테스트가 한도 명시 monkeypatch · 멱등 코어 다중 네임스페이스 표면 → post:create 전용
> 인라인(-56줄) · rate limit 경로 리터럴 → `app/common/paths.py` 상수 + 라우트 테이블 대조
> 드리프트 가드 테스트.
> **보류 유지(기록대로)**: 매니저 공용 베이스/락 통합, `_put_object` 테스트 헬퍼 통합.
---

### 36. 제품 결정 문서화 — P3

코드 수정이 아니라 "의도"를 남기는 항목.

- refresh 토큰 유저당 단일 키 = 단일 세션 정책(두 번째 기기 로그인 시 첫 기기 refresh 무효) 명시.
- WS 인증 토큰 쿼리스트링(`?token=`) 노출 트레이드오프.
- 트렌딩 `window_hours` 1~48 클라이언트 제어 → 캐시 키 분화. FE 사용값(24) 고정 검토.
- **(③ 마감 리뷰)** 차단 시맨틱: 차단 관계에서 `GET /chat/rooms/direct/{peer}`는 403(새 대화 진입 차단)이지만
  기존 room_id 기반 목록·기록·읽음은 동작(과거 대화 열람 허용) — 의도된 비대칭인지 결정하고 문서화.
  FE는 direct-open 403을 "차단한 상대" UX로 처리해야 함.


> **수정 완료**: 트렌딩 `window_hours` 클라이언트 제어는 문서화 대신 **제거·서버 고정 24h**로
> 결정(FE 사용값 24 하나 — 소비자 없는 표면이 캐시 키 분화+무거운 쿼리 남용 벡터, ADR 0004).
> 단일 세션(refresh 유저당 단일 키)·WS 토큰 쿼리스트링 트레이드오프·차단 시맨틱 비대칭(새 대화
> 403, 과거 열람 허용 — FE의 direct-open 403 처리 계약 포함)은
> [ADR 0013](adr/0013-product-behavior-decisions.md)으로 확정.

> **⑤ 마감 리뷰(8앵글 파인더+검증) 반영 완료**: ① psycopg v3는 예외에 `pgcode`가 없어(실측:
> `sqlstate`만 존재) IntegrityError 409 매핑·signup 중복 변환이 프로덕션 데드였다 —
> `sqlstate` 기반으로 교정(전역 핸들러+auth, 테스트도 실 속성으로) ② envelope 신→구 롤링
> 비호환 — 발행 시 구 스칼라 키 병기(다음 릴리스에서 제거) ③ `.env.example`의
> `VIEW_CACHE_TTL_SECONDS=0`이 폴백 제거로 실효되는 문제 — 주석 처리+경고, ADR 0007에
> dedup-끔 모드 기록 ④ `/v1/health`를 rate limit 스킵에서 제외(비인증 무한도 DB ping 표면 —
> 프로브는 /livez·/readyz 전담) ⑤ 인라인 SNS 태스크 셧다운 드레인(publish~마킹 사이 끊김의
> 이중 배송 창) ⑥ SNS 배송 안무 2벌 → `infra/sns.deliver_once` 단일 소스 ⑦ 트렌딩
> `window_hours` 서비스 파라미터·캐시 키 세그먼트 잔존 제거(24h 상수화) ⑧ view TTL 모듈
> 스냅샷 제거(settings 직접 참조) ⑨ 멱등 훅 검증기 2벌·fingerprint 3회 재계산 → before가
> fingerprint를 반환해 단일화. 검증 기각 4건(워커 루프 재생성 도달 불가·멱등 fail-open과
> loader 폴백은 문서화된 설계·DM 팬아웃 1건화는 at-most-once 수용 범위)은 수정 없음.
> **이연(저가치 정리, 해당 파일 작업 시 처리)**: bytes→str 정규화 3벌(infra/cache._decode·
> infra/redis.bulk_to_str·media 자체구현), 테스트 `_HitRedis` 2벌·경로 리터럴·통합테스트
> bare getattr 잔존, 라우터 인라인 `get_app_redis` vs `Depends` 관용구 2벌, FakeRedis
> 미구현 메서드 `__getattr__` 명시 실패 가드.
---

### 37. 실시간 전달 심화 (③ 마감 리뷰 이연) — P2

운영 봉투를 넘어서는 규모·적대 트래픽 대비. 현 시점엔 미정당 복잡도로 판단해 보류, 근거와 함께 기록.

- **큐 기반 소켓별 전달**: 공용 리스너가 로컬 전달을 직접 await하므로 정체 소켓이 리스너를 최대
  5s(send 타임아웃) 지연시킬 수 있다 — 소켓별 bounded 큐+writer 태스크로 격리하면 정체가 해당
  소켓에만 갇히고 유저별 순서도 유지된다. 태스크-per-envelope는 순서가 깨져 기각.
- ~~**유저당 WS 동시 연결 상한**~~ → **#42로 해소**(SSE까지 함께 상한). 억제 창은 연결 단위라,
  연결을 계속 새로 열면 연결마다 첫 거부 전 Redis 왕복이 발생하던 문제.
- envelope 수신자 목록 단일화·차단 EXISTS 통합은 #34에 기록됨.
- `get_current_user_optional`은 status 캐시 미적용(필수 인증 경로와 비대칭) — 의도 확인.
- 고아 해시태그 행 미GC(무해) 인지.

---

### 38. 댓글 블라인드가 게시글 `comment_count`를 조정하지 않음 — P1

**파일**: `app/domain/comments/repository.py`, `app/domain/comments/service.py`, `app/domain/reports/targets.py`

목록 조회는 블라인드 댓글을 제외하는데(`_reply_visible_conditions`) 블라인드 처리는 `is_blinded`만
세팅하고 `Post.comment_count`를 건드리지 않았다. 삭제 경로는 차감하는데 블라인드만 빠져 있어,
"댓글 5개"라고 표시하면서 4개만 보이는 불일치가 난다. 신고 임계값 자동 블라인드(`ReportService`)도
같은 경로라 사용자 행동만으로 드리프트가 쌓인다. 트렌딩 점수에도 가려진 댓글이 계속 기여했다.

**수정 방향**: `comment_count`를 "표시 가능한(미삭제·미블라인드) 댓글 수"로 정의하고, 블라인드
전이마다 ±1. 상태를 읽고-나서-쓰면 동시 모더레이션에서 이중 조정이 나므로 전이 판정은 원자적으로.

> **수정 완료**: 조건부 UPDATE + RETURNING(`blind_if_visible`·`unblind_if_blinded`)으로 전이를
> 원자적으로 판정하고, 전이가 실제 일어난 경우에만 게시글 카운트를 조정한다. 조정 주체는 생성·삭제
> 경로와 같은 서비스층(`CommentModeration`) — `reports/targets.py`의 모더레이션 배선이 COMMENT
> 타깃으로 이 파사드를 가리켜 `AdminService`·`ReportService`는 분기 없이 그대로 쓴다. 블라인드된
> 댓글을 삭제할 때의 이중 차감은 `delete_comment`가 **삭제 직전** `is_blinded`를 RETURNING하도록
> 바꿔 막았다. 블라인드·해제·reset 반복(멱등)과 이중 차감 부재를 통합 테스트로 고정.

---

### 39. 트렌딩 해시태그에 집계 창 부재 — P2

**파일**: `app/domain/posts/repository.py`, `app/domain/posts/services/hashtag_service.py`

`get_trending_hashtags`가 기간 조건 없이 전체 기간 누적 카운트를 셌다. "지금 뜨는 멍태그"라는
화면 문구와 어긋나고, 누적값은 시간이 갈수록 초기 태그에 고정돼 순위가 사실상 갱신되지 않는다.
tie-breaker도 없어 동점 태그 순서가 비결정적 — 캐시 갱신마다 목록이 뒤집혀 보인다.
트렌딩 게시글은 24h 창을 쓰는데(ADR 0004) 해시태그만 창이 없어 랭킹 철학도 불일치했다.

**수정 방향**: 게시글과 같은 서버 고정 24h 창 + 결정적 tie-breaker.

> **수정 완료**: `window_hours`(기본 24) 추가·`name ASC` tie-breaker·희소 시 전체 기간 1회 폴백.
> 창이 서버 고정이라 캐시 키 분화는 없다(ADR 0004의 `window_hours` 제거 결정과 동일 근거).
> 함께 트렌딩 점수식을 좋아요·조회수 중심으로 재조정하고(댓글 가중치 3→1) 24h 창과 이중 감쇠였던
> 지수를 1.3→1.0으로 완화. 가중치·지수는 매직넘버에서 명명 상수로 빼 근거를 코드에 남겼다.

---

## 3차 감사 (2026-08-04) — 운영 전제 위반 점검 #40~#43

> [`00 운영 봉투`](00-operating-envelope-and-scope.md)를 기준으로 전 기능을 재점검했다.
> 봉투 **초과**(과잉 복잡도)는 지적할 것이 없었다 — Celery 실배선·낙관적 락·SNS 멱등은
> 모두 ADR로 근거가 있다. 아래는 전부 봉투 **미달** 항목이다.
> #21(대댓글 상한)도 이 점검에서 함께 해소했다.

### 40. 주기 정리 잡에 분산 락 부재 — P1

**파일**: `app/core/cleanup.py`, `app/main.py`, `app/domain/media/service.py`, `app/infra/lock.py`

`run_periodic`이 락 없이 4개 잡을 각 인스턴스 타이머로 돈다. 봉투상 인스턴스가 3~10대이므로
같은 정리 잡이 인스턴스 수만큼 동시에 실행된다. 특히 `withdrawn_user_purge`·`notification_purge`는
테이블이 빌 때까지 청크 삭제를 반복하는 구조라, 10대면 같은 청크를 10번 집어 9번은 헛돌고
삭제 락 경합이 겹친다 — "청크별 `begin()`으로 부하를 끊는다"는 기존 완화가 N중 동시 실행으로
무력화된다. 조회수 flush와 미디어 잡에는 락이 있어 **패턴은 이미 있었는데 잡마다 각자 걸게 둔
구조가 빠뜨리기 쉬웠다**(실제로 둘을 빠뜨렸다).

**수정 방향**: 잡 락을 러너 층에서 잡별로 일괄 적용.

> **수정 완료**: `PeriodicJob`에 `lock_ttl_seconds`를 두고 `run_jobs_once`가 `lock:job:{name}`으로
> 획득→`finally` 해제한다(잡이 예외로 끝나도 해제 — 안 그러면 한 번의 실패가 TTL 동안 전
> 인스턴스를 막는다). 퍼지 2종은 오래 도므로 TTL 상향. media의 사설 헬퍼는
> `infra/lock.try_acquire_job_lock`으로 올려 공용화했고, media 자체 락은 **유지**한다 —
> `sweep_unused_images`가 업로드 응답 후 BackgroundTasks 경로에서도 불려 러너 락이 그 경로를
> 덮지 못한다. Redis 부재·장애는 기존 규약대로 락 없이 진행(미실행보다 중복 실행이 낫다).

---

### 41. 인덱스 마이그레이션이 전부 비-`CONCURRENTLY` — P1

**파일**: `migrations/versions/*`, `migrations/script.py.mako`

비-`CONCURRENTLY` `CREATE INDEX`는 대상 테이블에 SHARE 락을 잡아 빌드 내내 쓰기를 차단한다.
`posts.title`/`content`의 pg_trgm GIN처럼 무거운 인덱스면 라이브 테이블에서 사실상 쓰기 중단이고,
봉투의 *무중단 롤링 배포*·*99.9%*와 정면 충돌한다. 지금까지 사고가 없었던 건 구축기라 테이블이
비어 있었기 때문 — 데이터가 쌓인 뒤 같은 습관으로 인덱스를 추가하면 가장 나쁜 시점에 드러난다.

**수정 방향**: 기존 리비전은 두고(적용된 마이그레이션은 재실행되지 않아 소급 수정이 무의미),
앞으로의 규약을 근거화한다.

> **수정 완료**: [ADR 0015](adr/0015-index-migration-concurrently.md) — `postgresql_concurrently`
> + `if_not_exists`, `autocommit_block()` 필수(빠뜨리면 배포가 깨지는 지뢰), 실패 시 INVALID 인덱스
> 정리 절차, 빈 테이블·베이스라인 예외의 판정 기준. `script.py.mako`에 포인터를 남겨 작성 시점에
> 규약이 보이게 했다.

---

### 42. SSE·WS 유저당 동시 연결 수 상한 부재 — P3

**파일**: `app/domain/notifications/stream.py`·`service.py`·`router.py`, `app/domain/chat/manager.py`·`router.py`

두 팬아웃 매니저 모두 `_by_user`에 연결 수 제한 없이 등록한다. 큐는 bounded(`maxsize=100`)이고
드롭 정책도 있지만, 연결 수가 무제한이면 유저 한 명이 인스턴스 로컬 상태를 무한히 늘릴 수 있다 —
WS 메시지 한도는 이미 연 연결의 트래픽만 막지 연결 수는 못 막는다(#37에 이연돼 있던 항목).

> **수정 완료**: `REALTIME_MAX_CONNECTIONS_PER_USER`(기본 5)를 SSE·WS가 공유한다. WS는 close
> code 1008, SSE는 429. SSE 거절을 위해 등록을 스트림 시작 **전으로 분리**했다 — 제너레이터
> 안에서 거절하면 이미 200이 나간 뒤라 상태 코드를 바꿀 수 없다.

---

### 43. 차단 목록 API에 페이지네이션 부재 — P2

**파일**: `app/domain/users/model.py`·`service.py`·`router.py`·`schema.py`

`GET /users/me/blocks`가 LIMIT 없이 전량을 반환한다(`joinedload(profile_image)` 동반).
다른 목록은 전부 커서 페이지네이션인데([ADR 0002](adr/0002-cursor-pagination.md)) 여기만 예외였다.

> **수정 완료**: `CursorPage[BlockedUserItem]`로 전환. 정렬·커서 축은 `blocked_id` —
> `user_blocks`의 PK가 `(blocker_id, blocked_id)`라 필터+정렬이 **추가 인덱스 없이 PK로 커버**된다.
> 차단 시점(`created_at`) 정렬은 복합 커서 인코딩이 필요한데 이 목록에 그 복잡도는 정당화되지
> 않는다. 응답 계약·정렬 축이 바뀌어 FE 동반 수정 대상.

---

## 요약표

| 우선순위 | # | 항목 | 파일 |
|---------|---|------|------|
| **P0** | 1 | 스토리지 삭제 실패 시 DB 레코드도 삭제 | `app/domain/media/service.py` |
| **P0** | 2 | View flush 락 CAS 미적용 | `app/domain/posts/services/post_service.py` |
| **P0** | 3 | 회원가입 이메일/닉네임 중복 IntegrityError 미처리 | `app/domain/auth/service.py` |
| **P0** | 4 | ILIKE `%`/`_` 이스케이프 누락 | `app/domain/posts/repository.py` |
| **P1** | 5 | Admin 신고 목록 메모리 페이지네이션 버그 | `app/domain/admin/service.py` |
| **P1** | 6 | 댓글 트리 500건 하드 리밋 + 인메모리 페이지네이션 | `app/domain/comments/model.py`, `service.py` |
| **P1** | 7 | get_current_user 매 요청 DB 조회 미캐싱 | `app/api/dependencies/auth.py` |
| **P1** | 8 | 정지 시 Access Token 미즉시 무효화 | `app/domain/admin/service.py` |
| **P1** | 9 | bcrypt 이중 실행 (pepper 기본값 `""`) | `app/core/security.py` |
| **P2** | 10 | 커서 페이지네이션 + COUNT(*) 중복 비용 | `app/domain/posts/services/post_service.py` |
| **P2** | 11 | 게시글 목록에서 Dog 전체 로드 | `app/domain/posts/repository.py` |
| **P2** | 12 | `sync_post_hashtags` 5회 왕복 | `app/domain/posts/repository.py` |
| **P2** | 13 | `UserBlock` 중복 인덱스 | `app/domain/users/model.py` |
| **P2** | 14 | `Settings` 비pydantic + 검증 범위 협소 | `app/core/config.py` |
| **P2** | 15 | `CommentLikesModel` 메서드 중복 | `app/domain/comments/model.py` |
| **P2** | 16 | 미읽음 카운트 전체 테이블 스캔 | `app/domain/chat/service.py` |
| **P3** | 17 | `{v}` 리터럴 Redis 해시 태그 미작동 | `app/domain/posts/services/post_service.py` |
| **P3** | 18 | `_PG_UUID` 중복 정의 | `users/model.py`, `comments/model.py`, `posts/model.py` |
| **P3** | 19 | `get_room_peer_info` 채팅방 중복 조회 | `app/domain/chat/service.py` |
| **P3** | 20 | `from __future__ import annotations` 불일치 | 전역 |
| **P2** | 21 | 대댓글 배치 로드 상한 없음(대댓글 페이지네이션 부재) | `app/domain/comments/model.py` |
| **P1** | 22 | Celery 실경로 미배선(장식화) | `app/core/celery.py`, `app/worker/*` |
| **P1** | 23 | SSE pubsub 공유 풀 점유 → 풀 고갈 시 전면 fail-open | `app/domain/notifications/service.py` |
| **P2** | 24 | 업로드 경로 2벌(direct + presigned) | `app/domain/media/router.py` |
| **P2** | 25 | 조회수 기록 경로 2벌(GET 자동 + POST /view) | `app/domain/posts/routers/post_router.py` |
| **P3** | 26 | ORM 클래스 소속 도메인 불일치 | `app/domain/users/model.py` 외 |
| **P1** | 27 | 429가 CORS·메트릭·접근로그 바깥에서 종료 | `app/main.py` |
| **P0** | 28 | XFF 무검증 신뢰 → 조회수 조작 벡터 | `app/api/dependencies/client.py` |
| **P1** | 29 | record_post_view 풀 eager-load + master | `app/domain/posts/services/post_service.py` |
| **P1** | 30 | chat pubsub 리스너 재연결 부재 | `app/domain/chat/pubsub.py` |
| **P1** | 31 | presign 남용 방어·pending/ GC 부재 | `rate_limit.py`, `storage.py`, ADR 0010 |
| **P1** | 32 | WS DM rate limit·차단 검사 부재 | `rate_limit.py`, `chat/service.py` |
| **P2** | 33 | 트렌딩 wait-timeout 빈 목록 반환 | `app/infra/cache.py` |
| **P2** | 34 | 소품 모음(멱등 순서·SNS client·경로 표기 등) | 여러 파일 |
| **P3** | 35 | 죽은 코드 일괄(MySQL 잔재 등) | 여러 파일 |
| **P3** | 36 | 제품 결정 문서화(단일 세션 refresh 등) | docs |
| **P1** | 38 | 댓글 블라인드가 게시글 comment_count 미조정 | `comments/repository.py`, `service.py`, `reports/targets.py` |
| **P2** | 39 | 트렌딩 해시태그 집계 창 부재(+점수식 재조정) | `app/domain/posts/repository.py`, `services/hashtag_service.py` |
| **P1** | 40 | 주기 정리 잡 분산 락 부재(인스턴스별 중복 실행) | `app/core/cleanup.py`, `app/main.py`, `infra/lock.py` |
| **P1** | 41 | 인덱스 마이그레이션 비-CONCURRENTLY(배포 중 쓰기 차단) | `migrations/*`, ADR 0015 |
| **P2** | 43 | 차단 목록 페이지네이션 부재 | `app/domain/users/model.py`, `service.py`, `router.py` |
| **P3** | 42 | SSE·WS 유저당 연결 수 상한 부재 | `notifications/stream.py`, `chat/manager.py` |
