# ADR 0011 — 대표견: 전용 뷰 관계 + 부분 유니크 인덱스

- **상태**: 채택됨 (Accepted)
- **관련 코드**: `app/domain/users/model.py`(`User.representative_dog` 뷰 관계·`DogProfile`
  부분 유니크 인덱스 `uq_dog_profiles_owner_representative`),
  `app/domain/posts/repository.py`(`_post_author_and_content_loads`),
  `app/domain/comments/repository.py`(`_comment_author_loads`),
  `app/domain/dogs/service.py`(`upsert_dog_profile` 대표 배정 정규화),
  `migrations/versions/008_dog_representative_unique.py`

## 맥락 (Context)

작성자 응답(`AuthorInfo`·`CommentAuthorInfo`·`UserProfileResponse`)은 소유자의 **대표견 1마리**만
쓴다. [운영 봉투](../00-operating-envelope-and-scope.md)상 게시글·댓글 목록은 가장 뜨거운 경로라,
작성자의 강아지를 전부 로드하면 목록 크기에 비례해 낭비가 크다([backlog #11](../backlog.md)).

이를 줄이려 posts·comments는 `selectinload(User.dogs.and_(DogProfile.is_representative.is_(True)))`
로 대표견만 골라 로드했다. 그런데 `.and_()`는 **컬렉션 로드 자체를 필터**한다 — 그 `User` 인스턴스의
`User.dogs` 관계 속성이 대표견 1마리(또는 0마리)로 **truncate되어 세션 identity map에 캐시**된다.
같은 세션에서 `user.dogs` 전체를 기대하는 코드(프로필)나 대표견을 뽑던
`User.representative_dog` 프로퍼티가 이 잘린 컬렉션을 읽으면 **에러 없이 데이터가 누락**된다.
목록 최적화가 컬렉션 시맨틱을 오염시키는 **부분 컬렉션 트랩**이다.

여기에 더해 '소유자당 대표견 1마리' 불변식은 `set_representative`의 명령형 2-UPDATE(전체 해제 →
1개 지정)로만 보장되고 **DB 제약이 없었다**. 데이터가 어긋나면(대표견 2개) `uselist=False` 로더가
조용히 하나만 고르거나 경고를 낸다.

## 결정 (Decision)

**대표견을 `User.dogs`와 분리된 전용 뷰 관계로 로드하고, 단일 대표견 불변식을 DB 부분 유니크
인덱스로 승격**한다.

1. **전용 뷰 관계** — `User.representative_dog`를
   `primaryjoin=(owner_id == User.id AND is_representative)`, `uselist=False`, `viewonly=True`,
   `lazy="raise_on_sql"`로 매핑. `dogs`를 절대 덮어쓰지 않는 **별도 속성**이라 트랩이 소멸한다.
   `viewonly`라 영속·overlaps 검사에서 제외돼 `dogs`와 FK를 공유해도 경고가 없다.
2. **프로퍼티 → 관계** — `self.dogs`를 순회하던 `@property representative_dog`를 제거하고 관계를
   단일 출처로 삼는다. 대표견을 직렬화하는 **모든 경로가 관계를 명시 eager-load**한다: posts·comments
   핫패스는 `dogs`를 건드리지 않고 대표견만, 프로필(`get_user_by_id_with_dogs`)은 전체 `dogs`와
   대표견을 함께 로드한다(프로필은 둘 다 필요·콜드 경로).
3. **불변식 DB 승격** — `uq_dog_profiles_owner_representative ON dog_profiles(owner_id)
   WHERE is_representative` 부분 유니크 인덱스로 소유자당 대표견을 1개로 강제. `uselist=False`를
   정당화하고 이중 대표견 데이터 오염을 원천 차단한다.
4. **쓰기 경로 정규화** — 인덱스가 트랜잭션 중 statement마다 검사되므로, `upsert_dog_profile`의
   create/update 행은 `is_representative`를 **항상 False**로 넣고 대표 배정은 마지막
   `set_representative`(전체 False → 1개 True)에 일임한다. 인라인으로 True를 여러 행에 넣던
   기존 방식은 일시적 중복으로 인덱스가 거부한다.

## 트레이드오프 (Consequences)

**얻은 것**
- 부분 컬렉션 트랩 소멸 — 목록 최적화가 `dogs` 컬렉션 시맨틱을 오염시키지 않는다.
- 목록 핫패스는 소유자별 전체 강아지 대신 대표견 1행만 로드(N+1 없는 배치 selectin).
- 단일 대표견이 DB 불변식이 되어 어떤 경로로도(직접 SQL·버그) 깨지지 않는다.

**치른 비용**
- 프로필 경로는 대표견 행을 두 번 로드(전체 `dogs` selectin + 대표견 selectin). GET /users/me 등
  **콜드 per-user 경로**라 작은 SELECT 1회 추가는 정합성 대비 수용한다.
- 대표견 배정이 반드시 `set_representative`를 거쳐야 한다는 제약(쓰기 경로 funnel)이 생긴다 —
  단일 진입점이라 오히려 불변식 유지에 유리.

## 고려한 대안 (Alternatives)

| 대안 | 기각 사유 |
|------|-----------|
| `dogs.and_()` 필터 로드 유지 | 부분 컬렉션 트랩 — 목록 로드가 프로필의 `dogs` 전체를 오염 |
| 프로퍼티 유지 + 프로필만 전체 로드 | 목록에서 전체 dogs 로드(핫패스 낭비)로 되돌아가거나 트랩 잔존 |
| `uselist=True` (리스트)로 대표견 관계 | 응답은 1마리만 쓰는데 리스트 계약이 되어 소비측 분기 필요 |
| DB 인덱스 없이 `uselist=False` | 불변식이 명령형 코드에만 의존 → 데이터 오염 시 조용한 오작동 |

## 일부러 하지 않은 것 (Non-goals)

- **chat 대표견 로딩 통일**: `chat/service.py`는 aliased `DogProfile` outerjoin으로 스칼라 컬럼을
  projection한다. 이미 `User.dogs`를 건드리지 않아 트랩이 없고, 관계 로딩과 쿼리 형태가 달라
  억지로 통일하지 않는다("쓸 데·안 쓸 데 구분").
- **대표견 미지정 허용 여부 변경**: 대표견 목록 전체 교체 시 어떤 항목도 대표로 지정하지 않으면
  대표견이 없어지는 현행 시맨틱(전체 교체 계약)은 유지한다.
