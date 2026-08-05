"""공개 데모 계정·콘텐츠 시드.

로그인 화면의 "데모 계정으로 둘러보기"가 쓰는 계정을 만든다. 계정만 있으면 들어가서
빈 화면을 보게 되므로, 그 버튼이 약속하는 것(피드·댓글·좋아요·알림·DM)을 실제로 채운다.
값은 프론트의 `src/config.ts` DEMO_ACCOUNT와 **같아야 한다** — 바뀌면 양쪽을 함께 고친다.

리포지토리가 아니라 **서비스 레이어**를 통해 심는다. 카운터 증감·알림 생성 같은 파생
효과가 실제 사용 경로와 같은 코드로 만들어져야, 데모 화면이 운영 화면과 어긋나지 않는다.

멱등: 데모 계정이 이미 있으면 아무것도 하지 않는다. 중간 실패로 반쯤 심긴 상태라면
계정을 지우고 다시 돌리는 편이 빠르다(아래 --purge).

사용:
    uv run poe seed-demo
    uv run poe seed-demo --purge   # 데모 계정 4개와 그 콘텐츠를 지운다(개발용)
"""

import argparse
import asyncio
import sys
from collections import Counter
from datetime import date
from uuid import UUID

from sqlalchemy import select

from app.db.engine import AsyncSessionLocal
from app.db.statements import delete_rows
from app.domain.auth.schema import SignUpRequest
from app.domain.auth.service import AuthService
from app.domain.chat.schema import ChatMessageSend
from app.domain.chat.service import ChatService
from app.domain.comments.model import Comment
from app.domain.comments.repository import CommentsRepository
from app.domain.comments.schema import CommentCreateRequest
from app.domain.comments.service import CommentService
from app.domain.dogs.service import DogService
from app.domain.likes.service import LikeService
from app.domain.posts.model import Post
from app.domain.posts.repository import PostsRepository
from app.domain.posts.schemas import PostCreateRequest
from app.domain.posts.services import PostService
from app.domain.users.model import User, UsersRepository

DEMO_PASSWORD = "PuppyTalk!demo1"
DEMO_EMAIL = "demo@puppytalk.shop"

# (email, nickname, 강아지: 이름·견종·성별·생일)
_ACCOUNTS: list[tuple[str, str, tuple[str, str, str, date]]] = [
    (DEMO_EMAIL, "데모퍼피", ("뽀삐", "말티즈", "female", date(2022, 4, 17))),
    ("mint@puppytalk.shop", "민트맘", ("민트", "포메라니안", "female", date(2021, 9, 3))),
    ("coco@puppytalk.shop", "코코아빠", ("코코", "웰시코기", "male", date(2020, 6, 25))),
    ("bori@puppytalk.shop", "보리누나", ("보리", "시바견", "male", date(2023, 1, 12))),
]

_DEMO_EMAILS = [email for email, _, _ in _ACCOUNTS]


async def _ensure_user(db, email: str, nickname: str) -> UUID:
    """가입 후 id 반환. signup은 반환값이 없으므로 이메일로 다시 읽는다."""
    await AuthService.signup(
        SignUpRequest(email=email, password=DEMO_PASSWORD, nickname=nickname), db=db
    )
    async with db.begin():
        user = await UsersRepository.get_user_by_email(email, db)
    if user is None:  # pragma: no cover - 방금 만든 계정이 없을 수는 없다
        raise RuntimeError(f"가입 직후 조회 실패: {email}")
    return user.id


async def _add_dog(db, owner_id: UUID, dog: tuple[str, str, str, date]) -> None:
    name, breed, gender, birth = dog
    async with db.begin():
        await DogService.upsert_dog_profile(
            owner_id,
            [
                {
                    "name": name,
                    "breed": breed,
                    "gender": gender,
                    "birth_date": birth,
                    "is_representative": True,
                }
            ],
            db,
        )


# 진행 출력의 개수는 여기서 센다 — 본문에 숫자를 적어 두면 시드를 늘리거나 줄일 때
# 출력만 조용히 거짓말을 한다.
_counts: Counter[str] = Counter()


async def _post(db, author: UUID, title: str, content: str, category_id: int) -> UUID:
    _counts["post"] += 1
    return await PostService.create_post(
        author,
        PostCreateRequest(title=title, content=content, category_id=category_id),
        db,
    )


async def _comment(
    db, post_id: UUID, author: UUID, content: str, parent: UUID | None = None
) -> UUID:
    _counts["comment"] += 1
    data = await CommentService.create_comment(
        post_id, author, CommentCreateRequest(content=content, parent_id=parent), db=db
    )
    return data.id


async def _like_post(db, post_id: UUID, user_id: UUID) -> None:
    _counts["like"] += 1
    await LikeService.like_post(post_id, user_id, db)


async def _like_comment(db, comment_id: UUID, user_id: UUID) -> None:
    _counts["like"] += 1
    await LikeService.like_comment(comment_id, user_id, db)


async def _dm(db, sender: UUID, peer: UUID, content: str) -> None:
    _counts["dm"] += 1
    await ChatService.send_dm_from_ws(
        db,
        sender_id=sender,
        payload=ChatMessageSend(peer_user_id=peer, content=content),
        redis=None,
    )


async def _seed() -> int:
    async with AsyncSessionLocal() as db:
        async with db.begin():
            if await UsersRepository.get_user_by_email(DEMO_EMAIL, db) is not None:
                print(f"이미 시드되어 있습니다: {DEMO_EMAIL} (--purge 후 재실행 가능)")
                return 0

        user_ids: list[UUID] = []
        for email, nickname, dog in _ACCOUNTS:
            uid = await _ensure_user(db, email, nickname)
            await _add_dog(db, uid, dog)
            user_ids.append(uid)
        demo, mint, coco, bori = user_ids
        print(f"계정 {len(user_ids)}개 생성")

        # 데모 유저 글 — 알림·좋아요가 이 글들에 달려 "내 활동"이 채워진다.
        walk = await _post(
            db,
            demo,
            "산책 코스 추천받아요",
            "요즘 같은 날씨에 뽀삐랑 걷기 좋은 곳 있을까요? 사람 적은 데면 더 좋아요.",
            1,
        )
        snack = await _post(
            db, demo, "수제 간식 처음 만들어봤어요", "고구마랑 닭가슴살로 만들었는데 잘 먹네요!", 3
        )
        # 다른 사람 글 — 피드가 한 사람으로만 차 있으면 커뮤니티로 안 보인다.
        vaccine = await _post(
            db, mint, "예방접종 주기 어떻게 되나요", "5차까지 맞았는데 그다음이 헷갈려요.", 2
        )
        await _post(
            db, coco, "코기 털빠짐 실화인가요", "청소기를 하루에 두 번 돌리고 있습니다...", 1
        )
        await _post(db, bori, "강아지 유치원 후기", "3개월 다녀본 솔직한 후기 남겨요.", 4)
        await _post(db, mint, "사료 나눔합니다", "알러지 때문에 못 먹이게 된 사료 나눔해요.", 5)
        print(f"게시글 {_counts['post']}개 생성")

        # 데모 글에 달리는 댓글·대댓글 — 각각 데모 유저에게 알림이 쌓인다.
        c_walk = await _comment(
            db, walk, mint, "저희는 강변 산책로 자주 가요! 평일 아침이 한적해요."
        )
        await _comment(
            db, walk, coco, "공원 뒤쪽 흙길 추천드려요. 발바닥에 무리도 덜 가고요.", c_walk
        )
        await _comment(db, walk, bori, "저도 거기 가봤는데 좋더라고요.", c_walk)
        await _comment(db, walk, mint, "주말은 사람이 좀 많아요 참고하세요!", c_walk)
        await _comment(db, walk, coco, "야간에도 조명 있어서 괜찮습니다.", c_walk)
        await _comment(db, snack, bori, "레시피 공유해주실 수 있나요?")
        await _comment(db, snack, mint, "우와 금손이시네요")

        # 페이지네이션이 화면에 실제로 보이도록 한 글에 댓글을 넉넉히 단다.
        authors = [mint, coco, bori]
        for i in range(12):
            await _comment(db, vaccine, authors[i % 3], f"저희 아이는 {i + 1}주 간격으로 맞췄어요.")
        # 데모 유저도 남의 글에 흔적을 남긴다("내가 쓴 댓글" 탭이 비지 않게).
        await _comment(db, vaccine, demo, "저도 같은 게 궁금했는데 도움 됐어요!")
        print(f"댓글 {_counts['comment']}개 생성")

        for liker in (mint, coco, bori):
            await _like_post(db, walk, liker)
        await _like_post(db, snack, mint)
        await _like_post(db, vaccine, demo)
        await _like_comment(db, c_walk, demo)
        print(f"좋아요 {_counts['like']}개 생성")

        # DM — 상대가 먼저 보낸 것이 읽지 않음으로 남아 채팅 배지가 뜬다.
        await _dm(db, coco, demo, "안녕하세요! 산책 글 보고 연락드려요 :)")
        await _dm(db, coco, demo, "혹시 주말에 같이 산책하실 생각 있으세요?")
        await _dm(db, demo, coco, "좋아요! 토요일 오전 어떠세요?")
        print(f"DM {_counts['dm']}건 생성")

    # 비밀번호는 찍지 않는다. 값 자체는 로그인 화면에 공개되지만, 이름이 password인 것을
    # stdout에 흘리면 CI 로그·터미널 스크롤백에 그대로 남고 정적 분석에도 걸린다
    # (CodeQL py/clear-text-logging-sensitive-data). 값은 FE config.ts·README에 있다.
    print(f"\n완료 — 로그인 계정: {DEMO_EMAIL} (비밀번호는 README 참고)")
    return 0


async def _purge() -> int:
    """데모 계정과 **그 계정이 만든 콘텐츠**를 지운다(개발용, 시드를 다시 심기 위한 되감기).

    유저만 지우면 안 된다 — `posts.user_id`·`comments.author_id`는 SET NULL이라 글·댓글이
    작성자 없는 채로 살아남는다(실사용자 탈퇴에서는 그게 옳은 규약이다). 시드를 여러 번
    돌리면 "탈퇴한 사용자"의 유령 글이 계속 쌓이므로, 여기서는 콘텐츠까지 명시적으로 지운다.

    순서에 이유가 있다:
    1. like_count 차감 — FK는 좋아요 **행**만 지우고 대상의 비정규화 카운터는 모른다
       (UserService.purge_withdrawn_users가 문서화한 짝). 공개 데모 계정은 실사용자 글에도
       좋아요를 누를 수 있어, 건너뛰면 남의 글 카운트가 영구히 부풀어 있는다.
    2. 댓글 → 글 순서로 삭제(글 삭제는 그 글의 댓글을 CASCADE로 함께 지운다).
    3. 살아남은 남의 글은 comment_count를 다시 센다 — 블라인드 전이와 같은 재계산 경로.
    """
    async with AsyncSessionLocal() as db, db.begin():
        rows = (await db.execute(select(User.id).where(User.email.in_(_DEMO_EMAILS)))).scalars()
        user_ids = list(rows)
        if not user_ids:
            print("지울 데모 계정이 없습니다.")
            return 0

        await PostsRepository.decrement_like_counts_for_users(user_ids, db=db)
        await CommentsRepository.decrement_like_counts_for_users(user_ids, db=db)

        own_posts = list(
            (await db.execute(select(Post.id).where(Post.user_id.in_(user_ids)))).scalars()
        )
        # 데모 계정이 **남의 글**에 단 댓글 — 그 글은 살아남으므로 카운트를 되돌려야 한다.
        touched_posts = set(
            (
                await db.execute(
                    select(Comment.post_id).where(
                        Comment.author_id.in_(user_ids), Comment.post_id.notin_(own_posts or [None])
                    )
                )
            ).scalars()
        )

        comments_deleted = await delete_rows(db, Comment, [Comment.author_id.in_(user_ids)])
        posts_deleted = await delete_rows(db, Post, [Post.id.in_(own_posts or [None])])
        for post_id in touched_posts:
            await PostsRepository.set_comment_count(
                post_id, await CommentsRepository.count_visible_for_post(post_id, db=db), db=db
            )
        deleted = await UsersRepository.purge_users_by_ids(user_ids, db=db)

    print(
        f"데모 계정 {len(deleted)}개, 게시글 {posts_deleted}개, 댓글 {comments_deleted}개를 삭제했습니다."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="seed_demo", description="공개 데모 계정과 둘러볼 콘텐츠를 심는다."
    )
    parser.add_argument(
        "--purge", action="store_true", help="데모 계정과 콘텐츠를 삭제한다(개발용)"
    )
    args = parser.parse_args()
    try:
        return asyncio.run(_purge() if args.purge else _seed())
    except Exception as e:
        print(f"시드 실패: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
