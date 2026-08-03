# 게시글·post_images 데이터 접근. ORM은 .model 참조.

from datetime import timedelta
from typing import Any, NamedTuple
from uuid import UUID

from sqlalchemy import Row, Select, and_, delete, exists, false, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.common.exceptions import ConcurrentUpdateException, InvalidRequestException
from app.db.base_class import utc_now
from app.db.statements import update_one_returning
from app.domain.users.model import User, UserBlock, author_display_loads, author_not_blocked_clause

from .model import Category, Hashtag, Post, PostImage, post_hashtags
from .schemas.post_schema import POST_IMAGES_MAX

# pg_trgm: 라틴 3자 미만·한글 1음절·숫자 1자리는 인덱스 효율 저하 → 앱 레벨 거부.
POST_SEARCH_MIN_TOKEN_LEN = 3
_POST_SEARCH_MIN_TOKEN_LEN_HANGUL = 2
_POST_SEARCH_MIN_TOKEN_LEN_DIGIT = 2


class VisiblePost(NamedTuple):
    """가시성 검사를 통과한 게시글의 경량 뷰. author_id는 작성자 탈퇴(SET NULL) 시 None."""

    author_id: UUID | None


def _ensure_image_limit(image_ids: list[UUID] | None) -> None:
    """스키마 검증을 우회한 내부 호출(시드·백필 등) 방어 — 상한 정의는 스키마 한 곳."""
    if image_ids and len(image_ids) > POST_IMAGES_MAX:
        raise InvalidRequestException(f"이미지는 최대 {POST_IMAGES_MAX}장까지 첨부할 수 있습니다.")


class ParsedSearch(NamedTuple):
    """검증·파싱이 끝난 검색어. tag가 있으면 #태그 정확 매칭, 없으면 토큰 AND 검색.

    검증(validate_search_query)과 필터 적용(_apply_post_list_search_filter)이 같은
    파싱 결과를 소비해, 태그 정규화·토큰화 규칙이 두 곳에서 어긋날 일이 없다.
    """

    tag: str | None
    tokens: tuple[str, ...]


def _min_token_length(token: str) -> int:
    if any("가" <= ch <= "힣" for ch in token):
        return _POST_SEARCH_MIN_TOKEN_LEN_HANGUL
    if token.isdigit():
        return _POST_SEARCH_MIN_TOKEN_LEN_DIGIT
    return POST_SEARCH_MIN_TOKEN_LEN


def _is_token_too_short(token: str) -> bool:
    return len(token) < _min_token_length(token)


def validate_search_query(q: str | None) -> ParsedSearch | None:
    """검색어 정규화·길이 검증 후 파싱 결과 반환. #태그는 정확 매칭(길이 제한 별도)."""
    if not q or not (stripped := q.strip()):
        return None
    if stripped.startswith("#"):
        tag_name = stripped.lstrip("#").strip().lower()
        if not tag_name:
            raise InvalidRequestException("검색할 해시태그를 입력해주세요.")
        return ParsedSearch(tag=tag_name, tokens=())
    tokens = tuple(stripped.split())
    if any(_is_token_too_short(t) for t in tokens):
        raise InvalidRequestException(
            "검색어는 공백으로 구분된 각 단어가 최소 2글자(한글·숫자) 또는 3글자(영문) 이상이어야 합니다."
        )
    return ParsedSearch(tag=None, tokens=tokens)


_ILIKE_ESCAPE = "\\"


def _escape_ilike_token(token: str) -> str:
    """ILIKE 와일드카드 문자(%, _)와 이스케이프 문자 자체를 이스케이프한다."""
    return token.replace(_ILIKE_ESCAPE, _ILIKE_ESCAPE * 2).replace("%", "\\%").replace("_", "\\_")


def _hashtag_match_exists(name_predicate):
    """게시글에 name_predicate를 만족하는 해시태그가 달려 있는가(정확·부분 매칭 공용)."""
    return exists(
        select(literal(1))
        .select_from(post_hashtags)
        .join(Hashtag, Hashtag.id == post_hashtags.c.hashtag_id)
        .where(
            post_hashtags.c.post_id == Post.id,
            name_predicate,
        )
    )


def _token_match_clause(token: str):
    """단일 토큰: 제목 OR 본문 OR 해시태그명 부분 일치.

    EXPLAIN 검증 예:
      EXPLAIN (ANALYZE, BUFFERS)
      SELECT id FROM posts
      WHERE deleted_at IS NULL AND is_blinded = false
        AND (title ILIKE '%불닭%' OR content ILIKE '%불닭%');
    → Bitmap Index Scan on idx_posts_title_gin / idx_posts_content_gin (pg_trgm 활성 시).
    """
    pattern = f"%{_escape_ilike_token(token)}%"
    return or_(
        Post.title.ilike(pattern, escape=_ILIKE_ESCAPE),
        Post.content.ilike(pattern, escape=_ILIKE_ESCAPE),
        _hashtag_match_exists(Hashtag.name.ilike(pattern, escape=_ILIKE_ESCAPE)),
    )


def _apply_post_list_search_filter(stmt, *, search: ParsedSearch | None):
    """목록/카운트 공통: #태그 정확 매칭, 그 외 토큰 AND + ILIKE(pg_trgm GIN 활용)."""
    if search is None:
        return stmt
    if search.tag is not None:
        return stmt.where(_hashtag_match_exists(Hashtag.name == search.tag))
    return stmt.where(and_(*(_token_match_clause(token) for token in search.tokens)))


def _post_visible_conditions(current_user_id: UUID | None) -> list:
    """게시글 표시 조건: 미삭제·미블라인드·(차단 작성자 제외). Post.id 조건은 호출부가 더한다."""
    conds: list = [Post.deleted_at.is_(None), Post.is_blinded.is_(False)]
    not_blocked = author_not_blocked_clause(Post.user_id, current_user_id)
    if not_blocked is not None:
        conds.append(not_blocked)
    return conds


def _post_author_and_content_loads():
    """목록·상세 공통 eager load — 작성자(users 공용 로더) + 게시글 콘텐츠 관계."""
    return (
        author_display_loads(Post.user),
        joinedload(Post.category),
        selectinload(Post.hashtags),
        selectinload(Post.post_images).joinedload(PostImage.image),
    )


async def _update_post(
    db: AsyncSession,
    post_id: UUID,
    *,
    alive: bool = False,
    returning: Any = Post.id,
    **values,
):
    """posts 카운터·모더레이션 공통 UPDATE…RETURNING — updated_at 갱신 포함.

    core UPDATE는 version_id_col(version)을 증가시키지 않는다(원 시맨틱 유지).
    alive=True면 deleted_at IS NULL 가드를 건다.
    """
    conds: list = [Post.id == post_id]
    if alive:
        conds.append(Post.deleted_at.is_(None))
    values["updated_at"] = utc_now()
    return await update_one_returning(db, Post, conds, values, returning)


# 트렌딩 풀은 캐시 항목(_TrendingCacheItem)이 쓰는 컬럼만 로드한다 — content(Text, 최대
# 50KB/행)와 미사용 category 관계 로드를 제거해 풀 재계산 비용을 줄인다.
_TRENDING_POOL_COLS = (
    Post.id,
    Post.title,
    Post.category_id,
    Post.comment_count,
    Post.like_count,
    Post.view_count,
    Post.user_id,
)


class PostsModel:
    @classmethod
    async def get_category_ids(cls, *, db: AsyncSession) -> frozenset[int]:
        rows = await db.execute(select(Category.id))
        return frozenset(rows.scalars().all())

    @classmethod
    async def post_is_visible(
        cls,
        post_id: UUID,
        *,
        db: AsyncSession,
        current_user_id: UUID | None = None,
    ) -> bool:
        """삭제·블라인드·차단 관계만 확인. 상세 조회용 eager load 없음."""
        r = await db.execute(
            select(exists().where(Post.id == post_id, *_post_visible_conditions(current_user_id)))
        )
        return bool(r.scalar())

    @classmethod
    async def get_visible_post_author(
        cls,
        post_id: UUID,
        *,
        db: AsyncSession,
        current_user_id: UUID | None = None,
    ) -> VisiblePost | None:
        """가시성 확인과 작성자 조회를 1쿼리로. None=비가시(미존재·삭제·블라인드·차단)."""
        row = (
            await db.execute(
                select(Post.user_id).where(
                    Post.id == post_id, *_post_visible_conditions(current_user_id)
                )
            )
        ).one_or_none()
        return VisiblePost(author_id=row[0]) if row is not None else None

    @classmethod
    async def sync_post_hashtags(
        cls,
        post_id: UUID,
        hashtag_names: list[str],
        *,
        db: AsyncSession,
    ) -> None:
        await db.execute(delete(post_hashtags).where(post_hashtags.c.post_id == post_id))

        if not hashtag_names:
            return

        # 전체 이름을 멱등 upsert 후 id를 한 번에 조회한다. ON CONFLICT DO NOTHING의
        # RETURNING은 충돌(기존)분을 돌려주지 않으므로, 재조회 대신 upsert→SELECT 순으로
        # 두면 동시 삽입분까지 committed 상태로 모두 잡혀 태그 누락이 없다(5→4왕복).
        await db.execute(
            pg_insert(Hashtag)
            .values([{"name": n} for n in hashtag_names])
            .on_conflict_do_nothing(index_elements=[Hashtag.name])
        )
        rows = (
            await db.execute(
                select(Hashtag.id, Hashtag.name).where(Hashtag.name.in_(hashtag_names))
            )
        ).all()
        id_by_name = {name: hid for hid, name in rows}
        hashtag_ids = [id_by_name[n] for n in hashtag_names if n in id_by_name]

        if hashtag_ids:
            await db.execute(
                pg_insert(post_hashtags)
                .values([{"post_id": post_id, "hashtag_id": hid} for hid in hashtag_ids])
                .on_conflict_do_nothing(index_elements=["post_id", "hashtag_id"])
            )

    @classmethod
    async def create_post(
        cls,
        user_id: UUID,
        title: str,
        content: str,
        image_ids: list[UUID] | None = None,
        category_id: int | None = None,
        hashtag_names: list[str] | None = None,
        *,
        db: AsyncSession,
    ) -> UUID:
        _ensure_image_limit(image_ids)
        now = utc_now()
        post = Post(
            user_id=user_id,
            title=title,
            content=content,
            category_id=category_id,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        db.add(post)
        await db.flush()
        if image_ids:
            db.add_all(
                PostImage(post_id=post.id, image_id=iid, created_at=now) for iid in image_ids
            )
        if hashtag_names is not None:
            await cls.sync_post_hashtags(post.id, hashtag_names, db=db)
        return post.id

    @classmethod
    async def get_post_detail(
        cls,
        post_id: UUID,
        *,
        db: AsyncSession,
        current_user_id: UUID | None = None,
    ) -> tuple[Post, bool] | None:
        """상세 1건 + is_liked를 단일 쿼리로. 익명이면 is_liked는 항상 False."""
        if current_user_id is not None:
            from app.domain.likes.model import PostLike

            is_liked_expr = (
                exists(1).where(PostLike.post_id == Post.id, PostLike.user_id == current_user_id)
            ).label("is_liked")
        else:
            is_liked_expr = false().label("is_liked")
        stmt = (
            select(Post, is_liked_expr)
            .where(Post.id == post_id, *_post_visible_conditions(current_user_id))
            .options(*_post_author_and_content_loads())
        )
        row = (await db.execute(stmt)).unique().one_or_none()
        if row is None:
            return None
        return row[0], bool(row[1])

    @classmethod
    async def get_post_author_id(cls, post_id: UUID, db: AsyncSession) -> UUID | None:
        result = await db.execute(
            select(Post.user_id).where(Post.id == post_id, Post.deleted_at.is_(None))
        )
        return result.scalar_one_or_none()

    @classmethod
    async def get_titles_by_ids(cls, post_ids: list[UUID], db: AsyncSession) -> dict[UUID, str]:
        if not post_ids:
            return {}
        result = await db.execute(
            select(Post.id, Post.title).where(Post.id.in_(post_ids), Post.deleted_at.is_(None))
        )
        return {row[0]: row[1] for row in result.all()}

    @classmethod
    async def get_all_posts(
        cls,
        size: int = 20,
        *,
        db: AsyncSession,
        cursor: UUID | None = None,
        search: ParsedSearch | None = None,
        category_id: int | None = None,
        current_user_id: UUID | None = None,
    ) -> list[Post]:
        # UUIDv7 PK: ORDER BY id DESC + id < cursor는 PK B-Tree만으로 범위 스캔(추가 인덱스 불필요).
        stmt = (
            select(Post)
            .where(*_post_visible_conditions(current_user_id))
            .options(*_post_author_and_content_loads())
        )
        stmt = _apply_post_list_search_filter(stmt, search=search)
        if category_id is not None:
            stmt = stmt.where(Post.category_id == category_id)
        if cursor is not None:
            stmt = stmt.where(Post.id < cursor)
        stmt = stmt.order_by(Post.id.desc()).limit(size + 1)
        result = await db.execute(stmt)
        return list(result.unique().scalars().all())

    @classmethod
    async def get_trending_hashtags(
        cls,
        *,
        db: AsyncSession,
        limit: int = 10,
    ) -> list[tuple[str, int]]:
        count_expr = func.count(post_hashtags.c.post_id).label("count")
        stmt = (
            select(Hashtag.name, count_expr)
            .join(post_hashtags, Hashtag.id == post_hashtags.c.hashtag_id)
            .join(Post, Post.id == post_hashtags.c.post_id)
            .where(Post.deleted_at.is_(None), Post.is_blinded.is_(False))
            .group_by(Hashtag.id, Hashtag.name)
            .order_by(count_expr.desc())
            .limit(limit)
        )
        rows = (await db.execute(stmt)).all()
        return [(str(r[0]), int(r[1] or 0)) for r in rows]

    @classmethod
    def get_trending_posts_query(
        cls,
        *,
        window_hours: int | None = 24,
        category_id: int | None = None,
        current_user_id: UUID | None = None,
        limit: int = 10,
        use_time_decay: bool = True,
    ) -> Select[Any]:
        # created_at 하한을 최상단에 두어 idx_posts_feed_latest(created_at DESC, partial) 범위 스캔 유도.
        # window_hours=None 이면 기간 제한 없음(최종 fallback용).
        stmt = select(*_TRENDING_POOL_COLS).where(*_post_visible_conditions(current_user_id))
        if window_hours is not None:
            cutoff = utc_now() - timedelta(hours=window_hours)
            stmt = stmt.where(Post.created_at >= cutoff)
        if category_id is not None:
            stmt = stmt.where(Post.category_id == category_id)

        if use_time_decay:
            age_hours = func.extract("epoch", func.now() - Post.created_at) / 3600.0
            score = (
                Post.comment_count * 3 + Post.like_count * 2 + Post.view_count * 0.1
            ) / func.power(age_hours + 2, 1.3)
            stmt = stmt.order_by(score.desc(), Post.id.desc())
        else:
            stmt = stmt.order_by(
                Post.like_count.desc(),
                Post.comment_count.desc(),
                Post.id.desc(),
            )

        return stmt.limit(limit)

    @classmethod
    async def get_trending_posts(
        cls,
        *,
        db: AsyncSession,
        window_hours: int | None = 24,
        category_id: int | None = None,
        current_user_id: UUID | None = None,
        limit: int = 10,
        use_time_decay: bool = True,
    ) -> list[Row[Any]]:
        stmt = cls.get_trending_posts_query(
            window_hours=window_hours,
            category_id=category_id,
            current_user_id=current_user_id,
            limit=limit,
            use_time_decay=use_time_decay,
        )
        result = await db.execute(stmt)
        return list(result.all())

    @classmethod
    async def get_blocked_author_ids(cls, blocker_id: UUID, *, db: AsyncSession) -> set[UUID]:
        """blocker가 차단한 유저 id 집합. 캐시된 트렌딩 풀에 차단 필터를 요청별로 오버레이할 때 사용."""
        rows = await db.execute(
            select(UserBlock.blocked_id).where(UserBlock.blocker_id == blocker_id)
        )
        return set(rows.scalars().all())

    @classmethod
    async def update_post(
        cls,
        post_id: UUID,
        title: str | None = None,
        content: str | None = None,
        image_ids: list[UUID] | None = None,
        category_id: int | None = None,
        hashtag_names: list[str] | None = None,
        *,
        expected_version: int | None = None,
        db: AsyncSession,
    ) -> bool:
        """부분 수정. 반환 False=미존재. expected_version 불일치는 낙관적 락 충돌로 raise.

        블라인드된 글은 미존재와 동일하게 취급한다 — 모더레이션이 숨긴 글을 작성자가
        수정으로 되살리는 경로를 막는다(기존 동작 보존).
        """
        _ensure_image_limit(image_ids)
        now = utc_now()
        post_obj = (
            await db.execute(
                select(Post).where(
                    Post.id == post_id,
                    Post.deleted_at.is_(None),
                    Post.is_blinded.is_(False),
                )
            )
        ).scalar_one_or_none()
        if post_obj is None:
            return False
        if expected_version is not None and expected_version != post_obj.version:
            raise ConcurrentUpdateException(
                "다른 사용자가 이미 글을 수정했습니다. 최신 데이터를 확인해주세요."
            )

        if title is not None:
            post_obj.title = title
        if content is not None:
            post_obj.content = content
        if category_id is not None:
            post_obj.category_id = category_id
        post_obj.updated_at = now

        if hashtag_names is not None:
            await cls.sync_post_hashtags(post_id, hashtag_names, db=db)

        if image_ids is not None:
            old_result = await db.execute(
                select(PostImage.image_id).where(PostImage.post_id == post_id)
            )
            old_image_ids = set(old_result.scalars().all())
            new_image_ids = set(image_ids)
            to_add = new_image_ids - old_image_ids
            to_remove = old_image_ids - new_image_ids
            if to_add:
                db.add_all(
                    PostImage(post_id=post_id, image_id=iid, created_at=now) for iid in to_add
                )
            if to_remove:
                await db.execute(
                    delete(PostImage).where(
                        PostImage.post_id == post_id, PostImage.image_id.in_(to_remove)
                    )
                )
        return True

    @classmethod
    async def soft_delete(cls, post_id: UUID, db: AsyncSession) -> bool:
        """게시글 soft-delete + 이미지 링크 삭제 — 단일 애그리거트만. 댓글·좋아요
        캐스케이드는 PostService.delete_post가 조율한다(이름에서 캐스케이드 암시 제거)."""
        deleted = await _update_post(db, post_id, alive=True, deleted_at=utc_now()) is not None
        if not deleted:
            return False
        await db.execute(delete(PostImage).where(PostImage.post_id == post_id))
        return True

    @classmethod
    async def increment_view_count(cls, post_id: UUID, db: AsyncSession, *, delta: int = 1) -> bool:
        if delta <= 0:
            return True
        return (
            await _update_post(db, post_id, alive=True, view_count=Post.view_count + delta)
            is not None
        )

    @classmethod
    async def increment_report_count(cls, post_id: UUID, db: AsyncSession) -> int | None:
        return await _update_post(
            db,
            post_id,
            alive=True,
            returning=Post.report_count,
            report_count=Post.report_count + 1,
        )

    @classmethod
    async def set_blinded(cls, post_id: UUID, db: AsyncSession) -> bool:
        return await _update_post(db, post_id, alive=True, is_blinded=True) is not None

    @classmethod
    async def unblind_post(cls, post_id: UUID, db: AsyncSession) -> bool:
        return await _update_post(db, post_id, alive=True, is_blinded=False) is not None

    @classmethod
    async def reset_reports(cls, post_id: UUID, db: AsyncSession) -> bool:
        return (
            await _update_post(db, post_id, alive=True, report_count=0, is_blinded=False)
            is not None
        )

    @classmethod
    async def get_reported_by_ids(cls, post_ids: list[UUID], db: AsyncSession) -> list[Post]:
        """신고 목록 하이드레이션용 id 배치 조회. 응답은 제목·본문·작성자만 쓰므로
        작성자+프로필 이미지만 eager-load 한다(정렬·페이지는 UNION 쿼리가 담당)."""
        if not post_ids:
            return []
        result = await db.execute(
            select(Post)
            .where(Post.id.in_(post_ids))
            .options(joinedload(Post.user).joinedload(User.profile_image))
        )
        return list(result.unique().scalars().all())

    @classmethod
    async def get_like_count(cls, post_id: UUID, db: AsyncSession) -> int:
        result = await db.execute(select(Post.like_count).where(Post.id == post_id))
        return result.scalar_one_or_none() or 0

    @classmethod
    async def increment_like_count(cls, post_id: UUID, db: AsyncSession) -> int:
        row = await _update_post(
            db, post_id, returning=Post.like_count, like_count=Post.like_count + 1
        )
        return row if row is not None else 0

    @classmethod
    async def decrement_like_count(cls, post_id: UUID, db: AsyncSession) -> int:
        row = await _update_post(
            db,
            post_id,
            returning=Post.like_count,
            like_count=func.greatest(Post.like_count - 1, 0),
        )
        return row if row is not None else 0

    @classmethod
    async def increment_comment_count(cls, post_id: UUID, db: AsyncSession) -> bool:
        return await _update_post(db, post_id, comment_count=Post.comment_count + 1) is not None

    @classmethod
    async def decrement_comment_count(cls, post_id: UUID, db: AsyncSession) -> bool:
        return (
            await _update_post(db, post_id, comment_count=func.greatest(Post.comment_count - 1, 0))
            is not None
        )
