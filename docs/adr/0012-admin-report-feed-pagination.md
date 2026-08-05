# ADR 0012 — 관리자 신고 피드 페이지네이션: DB-side UNION ALL + offset 유지

- **상태**: 채택됨 (Accepted)
- **관련 코드**: `app/domain/admin/repository.py`(`AdminReportsRepository.page_reported_targets`),
  `app/domain/admin/service.py`(`get_reported_posts`), `app/domain/admin/router.py`(`GET /admin/reported-posts`),
  `app/domain/posts/repository.py`·`app/domain/comments/model.py`(`get_reported_by_ids` 하이드레이션),
  `app/domain/users/model.py`(`Report` — `ix_reports_target` 부분 인덱스)
- **관계**: [ADR 0002](0002-cursor-pagination.md)(cursor 표준)의 *예외*를 명시적으로 근거화한다.

## 맥락 (Context)

관리자 신고 목록(#5)은 신고된 **게시글과 댓글을 하나의 triage 피드**로 합쳐 내려준다.
기존 구현은 두 소스를 각각 `min(500, …)` offset으로 조회한 뒤 Python에서 `merged.sort()` →
`merged[start:start+size]`로 잘랐다. 결과:

- 500 cap 초과 페이지는 **무음 소실**,
- 소스는 `report_count DESC`로 정렬하는데 병합은 `last_reported_at`으로 재정렬해 **정렬 축 불일치**,
- 두 소스를 독립 offset 하므로 **전역 페이지 경계가 부정확**, `total`도 실제 노출과 어긋남.

[ADR 0002](0002-cursor-pagination.md)는 이 인메모리 슬라이스 버그(#5)를 지목하며 "admin을 DB keyset로"
옮기자고 적었다. 그러나 admin 피드는 [운영 봉투](../00-operating-envelope-and-scope.md)상 공개 피드와
**전제가 다르다** — 소수 관리자만 접근하는 **저트래픽 경로**이고, 정렬 축이 변동값(`report_count`)이며,
운영자에게 **대기열 크기(`total`)가 유용**하다. 그래서 0002가 지목한 *버그*는 고치되, *메커니즘*은
공개 피드(cursor)와 다르게 간다.

## 결정 (Decision)

**두 테이블을 DB-side `UNION ALL`로 합쳐 DB에서 정렬·페이지하고, offset + `count(*)`를 유지**한다.

1. **UNION ALL 페이지 쿼리** — `posts`·`comments`를 각각 `(target_type, id, report_count, created_at)`로
   투영해 `UNION ALL`, `ORDER BY report_count DESC, created_at DESC, id DESC LIMIT size OFFSET off`.
   **단일 ORDER BY**로 정렬 축 불일치를 없앤다. `total`은 같은 UNION 서브쿼리에 `COUNT(*)`를
   **따로** 돌려 얻는다(페이지 쿼리와 2문장 — 윈도우 집계로 합치면 `LIMIT`이 무력화된다.
   [ADR 0016](0016-comment-list-offset-pagination.md) 결정 3번에 근거를 적었다).
2. **하이드레이션** — 페이지의 `(type, id)`만 받아 posts/comments를 id 배치로 로드(`get_reported_by_ids`),
   report 집계는 기존 배치(`bulk_max_created_at`·`bulk_reasons`)를 재사용, **UNION이 정한 순서를 그대로
   유지**해 재정렬 없이 조립. 인메모리 병합·정렬·cap 전부 제거.
3. **응답 계약 유지** — `PaginatedResponse{items, has_more, total}`. cursor로 바꾸지 않는다.
4. **저자 없는 콘텐츠 제외** — SET NULL로 작성자가 사라진 신고 콘텐츠는 표시 대상이 아니므로 UNION
   `WHERE`에서 제외해 `total`과 노출 건수를 일치시킨다.
5. **`(target_type, target_id)` 부분 인덱스** — 집계·`delete_by_target`가 이 두 컬럼으로 조회하므로
   `WHERE deleted_at IS NULL` 부분 인덱스를 추가(마이그레이션 011).

즉 **ADR 0002의 목표(인메모리 슬라이스 제거·total/items 정합)는 이행**하되, 페이지네이션 메커니즘만
공개 피드=cursor / admin=offset+total로 **의도적으로 분기**한다.

## 트레이드오프 (Consequences)

**얻은 것**
- 페이지 경계·`total`이 DB에서 정확 — cap 무음 소실·정렬 축 불일치 제거.
- 운영자가 `total`로 신고 대기열 규모를 즉시 파악(triage에 실질 가치).
- `report_count DESC` triage 정렬을 유지 — "가장 많이 신고된 것부터".

**치른 비용**
- **깊은 페이지 offset 스캔 비용** — 저트래픽 admin이라 수용(공개 피드가 아니라 봉투상 무의미).
- **매 요청 `count(*) over union`** — 마찬가지로 저트래픽에서 수용.
- **offset 페이지-shift** — 조회 중 새 신고가 들어오면 경계가 밀릴 수 있으나, 관리자 큐는 다음 새로고침에
  self-correcting. cursor의 drift 회피 이점이 이 경로에선 정당화되지 않는다.

## 고려한 대안 (Alternatives)

| 대안 | 기각 사유 |
|------|-----------|
| cursor(id keyset) `CursorPage` | uuid7 id 정렬로 가면 **"많이 신고된 순" triage 정렬 상실**(최신 신고 콘텐츠 순이 됨). admin 저트래픽에 커서(deep-offset 회피) 이득 없음 |
| cursor(report_count 튜플 keyset) | `report_count`는 변동값 → keyset 경계 **드리프트**(중복·누락). comments 인기순 keyset을 같은 이유로 제거한 결정과 배치됨 (그 결정은 이후 [ADR 0016](0016-comment-list-offset-pagination.md)에서 *정렬 제거*가 아니라 *offset 전환*으로 대체됐다 — "변동 축에 keyset을 얹지 않는다"는 논거는 그대로 유지된다) |
| 두 엔드포인트 분리(reported-posts / reported-comments 각 단일 테이블 keyset) | 각각은 단순하나 **통합 triage 피드 UX를 깨고** FE 계약을 이원화 |
| 현행 인메모리 병합·슬라이스 | #5 버그 그 자체(cap 소실·total 불일치) |

## 일부러 하지 않은 것 (Non-goals)

- **공개 피드와의 cursor 일관성 강제**: 일관성은 목적이 아니라 봉투에 종속된다. 저트래픽·변동 정렬·
  total 필요라는 전제가 다르므로 여기선 offset이 정답 — "쓸 데와 안 쓸 데를 구분"의 사례.
- **`report_count` 실시간 keyset 페이지네이션**: 변동값 keyset의 드리프트를 admin에서 감수할 이유 없음.
- **임의 정렬 옵션(최신순/오래된순 등)**: 신고 triage는 `report_count DESC` 단일 축이면 충분.
