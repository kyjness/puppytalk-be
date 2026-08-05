# ADR 0016 — 댓글 목록: 인기순 복원을 위한 offset + `total` 전환

- **상태**: 채택됨 (Accepted)
- **관련 코드**: `app/domain/comments/repository.py`(`page_root_comments`·`_apply_id_keyset`),
  `app/domain/comments/service.py`(`get_comments`), `app/domain/comments/router.py`(`GET /posts/{id}/comments`),
  `app/domain/comments/model.py`(`idx_comments_post_popular`·`idx_comments_post_latest`),
  `migrations/versions/014_comment_root_sort_indexes.py`
- **관계**: [ADR 0002](0002-cursor-pagination.md)(cursor 표준)의 *예외*를 하나 더 근거화한다.
  [ADR 0012](0012-admin-report-feed-pagination.md)와 **같은 논거·같은 결론**이다.
  [ADR 0015](0015-index-migration-concurrently.md)의 인덱스 규약을 따른다.

## 맥락 (Context)

**인기순(좋아요 기준) 댓글 정렬을 제품 요구로 되살려야 한다.** 이 정렬은 한 번
제거된 기능이다 — [ROADMAP](../ROADMAP.md) #6에서 루트 목록을 keyset로 옮기면서
"좋아요 keyset 드리프트 부정당"을 이유로 뺐고, [ADR 0012](0012-admin-report-feed-pagination.md)는
그 결정을 선례로 인용하며 admin 피드의 튜플 keyset을 기각했다.

그 논거 자체는 지금도 옳다. `like_count`는 **변동값**이라 `(like_count, id)` 커서는
경계가 흔들린다 — 1쪽을 본 뒤 경계 댓글에 좋아요가 하나 눌리면 그 댓글이 2쪽에 다시
나오거나 아예 건너뛰어진다. 커서 페이지네이션의 존재 이유가 바로 그 드리프트 회피인데,
변동 축에 얹으면 스스로를 무효화한다.

동시에, 커서로 바꾸면서 FE의 **번호 페이지 UI가 죽어 있었다**. 서버가 `total`을 주지
않으니 `totalPages`가 항상 1이라 nav가 렌더되지 않았고, 11번째 댓글부터 도달할 방법이
없었다(FE `4dbea9b`에서 "더 보기"로 대체). 즉 요구는 둘이다: **인기순**과 **페이지 번호**.

## 결정 (Decision)

**루트 댓글 목록을 offset + `total`(`PaginatedResponse`)로 전환하고, 대댓글은 커서를 유지한다.**

1. **루트 목록** — `GET /posts/{id}/comments?page=&size=&sort=latest|popular`.
   `page_root_comments(offset, size)`가 한 페이지와 전체 건수를 함께 낸다.
   `has_more = (page * size) < total`.
2. **정렬 축** — 인기순 `like_count DESC, id DESC` / 최신순 `id DESC`. 어느 쪽이든 **불변인
   id를 타이브레이커**로 붙여 전순서를 만든다 — 같은 `like_count` 안에서 순서가 흔들리면
   offset 경계가 새어 나간다. 기본값은 최신순, 알 수 없는 값도 최신순으로 떨어진다.
3. **`total`은 COUNT 쿼리를 따로 돈다** — 페이지 쿼리와 2문장이다
   ([ADR 0012](0012-admin-report-feed-pagination.md)의 admin 피드와 같은 형태).
   한 문장으로 줄이려고 `count() over ()`를 얹는 것은 **틀린 최적화**다: 빈 PARTITION의
   윈도우 집계는 첫 행을 내보내기 전에 매칭 행을 전부 소비하므로 `LIMIT`이 무력화되고,
   출력이 정렬을 잃어 Sort 노드까지 생긴다(실측 플랜: `WindowAgg → Sort → Limit`).
   행마다 작성자 JOIN 2개와 상관 EXISTS가 붙는 스캔이라 그 차이가 곧 비용이다.
   분리하면 페이지 쿼리는 `Index Scan Backward`로 `offset+size`에서 끊기고, COUNT는
   eager load·ORDER BY 없이 조건만 센다.
4. **대댓글(`GET /{comment_id}/replies`)은 그대로 커서** — 정렬 축(`parent_id`·`id`)이
   불변이고 루트당 소량이다. 커서의 이점이 그대로 성립하는 유일한 곳이라 바꾸지 않는다.
   `sort=popular`는 대댓글에서 최신순으로 매핑된다 — 대댓글엔 좋아요 UI 자체가 없다.
5. **정렬 인덱스 2개**(마이그레이션 014, CONCURRENTLY) — offset 페이지는 요청마다 정렬을
   타므로 순서까지 인덱스가 받는다. 부분 술어는 쿼리의 `parent_id IS NULL AND is_blinded IS FALSE`와
   일치시킨다. `deleted_at`은 넣지 않는다 — 삭제 루트도 표시 가능한 대댓글이 있으면
   placeholder로 목록에 남는다.

## 트레이드오프 (Consequences)

**얻은 것**
- 인기순이 **드리프트 없이** 성립한다. 변동 축에 keyset을 얹지 않는다는 0012의 원칙을 지키면서
  기능을 되살렸다.
- 번호 페이지가 **실제로 동작**한다(`total`이 있으므로). 임의 페이지로 점프할 수 있다.
- 정렬 축이 인덱스로 덮여, 커서 시절 `ix_comments_post_id`만으로 파티션을 읽고 정렬하던
  경로가 사라졌다.

**치른 비용**
- **깊은 offset 스캔** — 댓글은 게시글 1건에 국한된 유한 집합이라 페이지 깊이에 실질 상한이
  있다. 공개 피드(무한 스크롤, 전역 집합)와 전제가 다르다.
- **매 요청 COUNT 1회 추가** — 왕복이 하나 는다. 대신 페이지 쿼리가 인덱스로 조기 종료되므로
  전체로는 싸다(위 결정 3번).
- **좋아요 쓰기 비용** — `idx_comments_post_popular`가 변동 컬럼 `like_count`를 포함하므로
  좋아요 UPDATE가 HOT 업데이트를 못 탄다(모든 인덱스에 새 엔트리 + 블로트). 인기순 읽기의
  실제 가격이며, 좋아요가 목록 조회보다 잦아지면 재검토 지점이다.
- **offset 페이지-shift** — 조회 중 새 댓글이 달리면 경계가 한 칸 밀릴 수 있다. 다음 새로고침에
  self-correcting이며, 인기순에서 커서가 겪을 중복·누락보다 작은 문제다.
- 목록 계약이 `CursorPage` → `PaginatedResponse`로 바뀐다. FE 타입 재생성으로 호출부가
  컴파일에서 잡힌다.

## 고려한 대안 (Alternatives)

| 대안 | 기각 사유 |
|------|-----------|
| `(like_count, id)` 튜플 keyset | 변동 축 keyset의 **경계 드리프트**(중복·누락). 0012가 admin에서 같은 이유로 기각한 것과 같은 형태 |
| 커서 유지 + 인기순만 offset | 한 엔드포인트가 정렬값에 따라 다른 페이지네이션 계약을 갖는다 — FE가 `sort`를 보고 파라미터를 갈아타야 하고, 계약 드리프트가 다시 생긴다 |
| 인기순 포기(현행 유지) | 제품 요구를 기술 제약으로 되돌려 보내는 것. 메커니즘을 바꾸면 풀리는 문제다 |
| 전체 로드 후 인메모리 정렬 | #6에서 제거한 인메모리 슬라이스·cap 그 자체. 인기 스레드에서 응답이 무한정 커진다 |

## 일부러 하지 않은 것 (Non-goals)

- **공개 피드(posts)의 커서 전환**: 전역·무한 집합이고 정렬 축이 불변(`id`)이라 커서가
  정답인 곳이다. [ADR 0002](0002-cursor-pagination.md)는 그대로 유효하다.
- **대댓글 인기순**: 좋아요 UI가 루트에만 있다. 축이 없는데 정렬만 만들지 않는다.
- **등록순(oldest) 탭**: 정렬 탭을 인기순·최신순 둘로 줄이는 제품 결정에 따라 루트 목록
  계약에서 뺐다. `/replies`의 `latest|oldest`는 대댓글 내부 순서용으로 남는다.
