"""데모 데이터 시드 — 공개 배포된 사이트를 "둘러볼 수 있는 상태"로 만든다.

배포 직후 DB에는 카테고리(migrations/versions/002_seed_categories.py)만 있고 글이 0건이다.
그 상태로는 커서 페이지네이션·무한스크롤·검색·조회수·해시태그·DM·알림 중 무엇도 보여줄 수
없으므로, 방문자가 기능을 확인할 수 있는 최소한의 콘텐츠를 심는다.

실행:
    uv run poe seed-demo                 # 로컬
    uv run poe seed-demo --force         # 지우고 다시 만들기
    docker compose exec backend python -m app.scripts.seed_demo   # 배포된 컨테이너 안에서

설계상 지켜야 하는 두 가지:

1. **오래된 것부터 삽입한다.** 목록은 `ORDER BY posts.id DESC`(uuid7 PK) 커서 페이지네이션이라
   (posts/repository.py) 정렬축이 id다. created_at만 과거로 흩뿌리면 "표시된 작성일"과 "목록
   순서"가 어긋난다. 시간순으로 넣으면 uuid7 id도 같은 순서로 증가해 둘이 일치한다.
2. **일부는 최근 24시간 안에 둔다.** 트렌딩 게시글·해시태그의 집계 창이 24시간이라
   (`get_trending_posts_query`), 전부 과거에 두면 인기글 영역이 빈 채로 보인다.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import struct
import zlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import NotificationKind
from app.core.config import settings
from app.core.ids import new_ulid_str
from app.core.security import hash_password, password_with_pepper
from app.db import AsyncSessionLocal
from app.domain.chat.model import ChatMessage, ChatRoom, normalize_dm_user_ids
from app.domain.comments.model import Comment, CommentLike
from app.domain.comments.repository import CommentsRepository
from app.domain.dogs.model import DogProfile
from app.domain.likes.model import PostLike
from app.domain.media.model import Image
from app.domain.notifications.model import Notification
from app.domain.posts.model import Hashtag, Post, PostImage, post_hashtags
from app.domain.posts.repository import PostsRepository
from app.domain.users.model import User
from app.infra.storage import build_url

# ---------------------------------------------------------------------------
# 데모 계정 — 이 목록이 "무엇이 데모 데이터인가"의 단일 정의처다(멱등·--force 판정 기준).
# 비밀번호는 공개 정보이며 프론트 로그인 화면에도 같은 값이 안내된다.
# ---------------------------------------------------------------------------
DEMO_PASSWORD = "PuppyTalk!demo1"

DEMO_USERS: list[dict[str, str]] = [
    {"email": "demo@puppytalk.shop", "nickname": "몽실이집사"},
    {"email": "demo2@puppytalk.shop", "nickname": "초코아빠"},
    {"email": "demo3@puppytalk.shop", "nickname": "코코엄마"},
    {"email": "demo4@puppytalk.shop", "nickname": "산책왕보리"},
    {"email": "demo5@puppytalk.shop", "nickname": "간식창고"},
    {"email": "demo6@puppytalk.shop", "nickname": "댕댕이라이프"},
    {"email": "demo7@puppytalk.shop", "nickname": "포메사랑"},
    {"email": "demo8@puppytalk.shop", "nickname": "말티즈맘"},
]

# gender 는 app.common.enums.DogGender 와 같은 소문자 값이어야 한다.
DEMO_DOGS: list[tuple[str, str, str]] = [
    ("몽실이", "말티즈", "female"),
    ("초코", "푸들", "male"),
    ("코코", "포메라니안", "female"),
    ("보리", "웰시코기", "male"),
    ("두부", "비숑프리제", "female"),
    ("호두", "시바견", "male"),
    ("설탕", "치와와", "female"),
    ("바둑이", "진돗개", "male"),
    ("루비", "골든리트리버", "female"),
    ("감자", "닥스훈트", "male"),
]

# 카테고리 id는 002_seed_categories가 고정한 값(1 자유 · 2 질문 · 3 자랑 · 4 정보 · 5 나눔).
POST_TEMPLATES: list[tuple[int, str, str]] = [
    (1, "{dog} 오늘 산책 다녀왔어요", "날씨가 좋아서 {dog}랑 한 시간이나 걸었네요."),
    (1, "{dog} 잠버릇이 너무 웃겨요", "배를 훌렁 뒤집고 자는데 볼 때마다 웃음이 나요."),
    (1, "비 오는 날 산책 어떻게 하세요", "{dog}는 우비를 싫어해서 현관에서부터 버팁니다."),
    (2, "{dog} 사료 바꾸는 시기 질문드려요", "8개월째 같은 사료인데 바꿔야 할까요?"),
    (2, "발톱 깎다가 피가 났어요", "{dog} 발톱을 깎다 혈관을 건드린 것 같습니다."),
    (2, "중성화 수술 시기 언제가 좋을까요", "{dog}가 이제 8개월인데 의견이 갈리네요."),
    (2, "분리불안 훈련 방법 여쭤봐요", "출근만 하면 {dog}가 하울링을 한다고 하네요."),
    (3, "{dog} 미용하고 왔습니다", "가위컷으로 부탁드렸는데 인형이 됐어요."),
    (3, "우리 {dog} 생일이에요", "오늘로 세 살이 됐습니다. 케이크 앞에서 얌전하네요."),
    (3, "{dog} 처음으로 앉아 성공", "간식 세 개 만에 앉았습니다. 천재가 아닐까요."),
    (3, "{dog}랑 카페 다녀왔어요", "애견동반 카페에 처음 갔는데 잘 어울리더라고요."),
    (4, "{dog} 다녀온 동물병원 후기", "야간 진료가 되는 곳이라 급할 때 도움이 됐습니다."),
    (4, "털 안 날리게 하는 빗 추천", "슬리커 브러시로 바꾸고 청소 시간이 반으로 줄었어요."),
    (4, "여름철 산책 시간 정리해봤어요", "손등을 5초 대보고 뜨거우면 그 시간은 피하세요."),
    (4, "강아지 치석 관리 이렇게 합니다", "거즈로 시작했는데 지금은 칫솔도 받아들입니다."),
    (5, "{dog}가 안 먹는 간식 나눔합니다", "유통기한 넉넉하고 미개봉이에요."),
    (5, "안 쓰는 하네스 나눔해요", "{dog}가 살이 쪄서 못 쓰게 됐습니다. 소형견용이에요."),
    (5, "배변패드 나눔합니다", "브랜드가 안 맞아서 그대로 남았어요."),
]

POST_TAILS: list[str] = [
    "혹시 비슷한 경험 있으신 분 계실까요?",
    "다들 어떻게 하고 계신지 궁금하네요.",
    "사진은 댓글에 더 올려볼게요.",
    "도움이 되셨으면 좋겠습니다.",
    "질문 있으시면 편하게 남겨주세요.",
    "다음에 후기 또 올리겠습니다.",
]

HASHTAGS: list[str] = [
    "산책",
    "강아지간식",
    "말티즈",
    "푸들",
    "포메라니안",
    "웰시코기",
    "비숑프리제",
    "댕스타그램",
    "강아지미용",
    "동물병원",
    "분리불안",
    "훈련",
    "나눔",
    "댕린이",
    "애견카페",
]

COMMENT_TEMPLATES: list[str] = [
    "너무 귀여워요! 저도 한 마리 더 키우고 싶어지네요.",
    "저희 집 아이도 똑같아요. 반가워서 댓글 남깁니다.",
    "정보 감사합니다. 오늘 바로 해볼게요.",
    "혹시 어디 제품 쓰시는지 여쭤봐도 될까요?",
    "사진만 봐도 힐링되네요.",
    "저도 같은 고민이었는데 도움이 많이 됐어요.",
    "산책 코스 좋아 보이네요. 어디쯤인가요?",
    "우와 미용 정말 잘 됐네요!",
    "이런 글 너무 좋습니다. 자주 올려주세요.",
    "저는 병원 다녀왔더니 금방 나았어요. 참고하세요.",
    "댓글 보고 저도 시도해봤는데 성공했습니다.",
    "완전 천재견인데요?",
]

REPLY_TEMPLATES: list[str] = [
    "감사합니다! 다음에 더 올려볼게요.",
    "제품은 쪽지로 알려드릴게요.",
    "네 맞아요, 그 방법이 제일 편하더라고요.",
    "한강 근처예요. 저녁에 가시면 시원합니다.",
    "저도 처음엔 힘들었는데 금방 익숙해지실 거예요.",
]

DM_SCRIPT: list[tuple[int, str]] = [
    (0, "안녕하세요! 나눔 글 보고 연락드려요."),
    (1, "안녕하세요~ 아직 남아있습니다!"),
    (0, "혹시 이번 주말에 직거래 가능할까요?"),
    (1, "네 토요일 오후면 괜찮아요."),
    (0, "그럼 토요일 3시에 뵐게요. 장소는 어디가 편하세요?"),
    (1, "공원 정문 쪽이 주차가 편해요."),
    (0, "좋아요! 그때 뵙겠습니다."),
    (1, "혹시 강아지도 데려오시나요?"),
    (0, "네 같이 갈 것 같아요. 산책 겸해서요."),
    (1, "잘됐네요, 저희 애도 데려갈게요."),
    (0, "좋습니다! 사이좋게 지냈으면 좋겠네요."),
    (1, "그럼 토요일에 봬요~"),
]

# 이미지 자리표시자 색상(위→아래 그라데이션).
IMAGE_PALETTE: list[tuple[tuple[int, int, int], tuple[int, int, int]]] = [
    ((255, 214, 165), (255, 173, 173)),
    ((189, 224, 254), (162, 210, 255)),
    ((202, 255, 191), (155, 246, 255)),
    ((255, 198, 255), (224, 187, 228)),
    ((253, 255, 182), (255, 214, 165)),
    ((205, 180, 219), (189, 224, 254)),
]

RGB = tuple[int, int, int]


# ---------------------------------------------------------------------------
# 이미지 — 저작권 걱정 없는 자리표시자를 코드로 만든다(외부 파일·의존성 없음).
# ---------------------------------------------------------------------------
def _png_chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def gradient_png(width: int, height: int, top: RGB, bottom: RGB) -> bytes:
    """세로 그라데이션 PNG 한 장. Pillow 없이 표준 라이브러리만으로 만든다."""
    rows: list[bytes] = []
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = bytes(round(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3))
        rows.append(b"\x00" + color * width)  # 행 앞의 0 = 필터 타입 None
    idat = zlib.compress(b"".join(rows), 6)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8비트 트루컬러
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def make_s3_client() -> Any:
    """app/infra/storage.py 와 같은 규칙으로 구성(커스텀 엔드포인트면 path-style)."""
    import boto3
    from botocore.config import Config

    kwargs: dict[str, Any] = {
        "region_name": settings.AWS_REGION,
        "aws_access_key_id": settings.AWS_ACCESS_KEY_ID,
        "aws_secret_access_key": settings.AWS_SECRET_ACCESS_KEY,
    }
    if settings.S3_ENDPOINT_URL:
        kwargs["endpoint_url"] = settings.S3_ENDPOINT_URL
        kwargs["config"] = Config(s3={"addressing_style": "path"})
    return boto3.client("s3", **kwargs)


class ImageUploader:
    """자리표시자 이미지를 S3에 올리고 Image 행을 만든다.

    키 형식은 승격된 이미지와 동일한 `{purpose}/{ulid}.{ext}`(storage.promote_pending_object) —
    운영 정리 작업(sweeper·lifecycle)이 데모 이미지도 같은 규칙으로 다루게 하려는 것.
    업로드가 한 번 실패하면 이후는 건너뛴다 — 스토리지가 없어도 텍스트 데이터는 심는다.
    """

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._client: Any = None

    async def create(
        self,
        db: AsyncSession,
        *,
        purpose: str,
        palette_index: int,
        size: tuple[int, int],
        uploader_id: UUID | None,
        now: datetime,
    ) -> Image | None:
        if not self.enabled:
            return None
        top, bottom = IMAGE_PALETTE[palette_index % len(IMAGE_PALETTE)]
        body = gradient_png(size[0], size[1], top, bottom)
        file_key = f"{purpose}/{new_ulid_str()}.png"
        try:
            if self._client is None:
                self._client = make_s3_client()
            await asyncio.to_thread(
                self._client.put_object,
                Bucket=settings.S3_BUCKET_NAME,
                Key=f"media/{file_key}",
                Body=body,
                ContentType="image/png",
            )
        except Exception as e:
            self.enabled = False
            print(f"  ! 이미지 업로드 실패 — 이미지 없이 계속합니다: {e}")
            return None
        image = Image(
            file_key=file_key,
            file_url=build_url(file_key),
            content_type="image/png",
            size=len(body),
            uploader_id=uploader_id,
            created_at=now,
        )
        db.add(image)
        await db.flush()
        return image


def post_timestamps(count: int, rng: random.Random) -> list[datetime]:
    """오래된 것부터 정렬된 작성 시각. 30%는 최근 24시간 안에 둔다(트렌딩 창)."""
    now = datetime.now(UTC)
    recent = max(1, count * 3 // 10)
    stamps = [now - timedelta(minutes=rng.randint(10, 23 * 60)) for _ in range(recent)]
    stamps += [now - timedelta(hours=rng.randint(25, 30 * 24)) for _ in range(count - recent)]
    stamps.sort()
    return stamps


# ---------------------------------------------------------------------------
# 정리(--force)
# ---------------------------------------------------------------------------
async def purge_demo_data(db: AsyncSession) -> None:
    """데모 계정과 그들이 만든 것을 지운다.

    삭제 순서가 곧 제약 조건이다 — post_images.post_id 는 ondelete=RESTRICT 라 게시글보다
    먼저 지워야 하고, posts.user_id 는 SET NULL 이라 유저만 지우면 글이 작성자 없이 남는다.
    """
    emails = [u["email"] for u in DEMO_USERS]
    user_ids = list((await db.execute(select(User.id).where(User.email.in_(emails)))).scalars())
    if not user_ids:
        return

    # 데모 계정이 **남의 글·댓글**에 누른 좋아요를 먼저 되돌린다. FK는 좋아요 행만 지우고
    # 대상의 비정규화 like_count는 모른다(UserService.purge_withdrawn_users가 문서화한 짝).
    # 데모끼리는 아래에서 글째로 지워지니 무관하지만, 공개 데모 계정은 방문자가 그대로
    # 로그인해 쓰는 계정이라 남의 콘텐츠에도 흔적을 남길 수 있다.
    await PostsRepository.decrement_like_counts_for_users(user_ids, db=db)
    await CommentsRepository.decrement_like_counts_for_users(user_ids, db=db)

    post_rows = await db.execute(select(Post.id).where(Post.user_id.in_(user_ids)))
    post_ids = list(post_rows.scalars())
    image_ids: list[UUID] = []

    if post_ids:
        rows = await db.execute(select(PostImage.image_id).where(PostImage.post_id.in_(post_ids)))
        image_ids += list(rows.scalars())
        await db.execute(delete(PostImage).where(PostImage.post_id.in_(post_ids)))
        # 댓글·좋아요·알림은 posts FK의 CASCADE가 함께 지운다.
        await db.execute(delete(Post).where(Post.id.in_(post_ids)))

    rows = await db.execute(
        select(User.profile_image_id).where(
            User.id.in_(user_ids), User.profile_image_id.is_not(None)
        )
    )
    image_ids += [i for i in rows.scalars() if i is not None]
    rows = await db.execute(
        select(DogProfile.profile_image_id).where(
            DogProfile.owner_id.in_(user_ids), DogProfile.profile_image_id.is_not(None)
        )
    )
    image_ids += [i for i in rows.scalars() if i is not None]

    # 강아지·채팅·알림은 users FK의 CASCADE로, 이미지 참조는 SET NULL 로 풀린다.
    await db.execute(delete(User).where(User.id.in_(user_ids)))
    if image_ids:
        await db.execute(delete(Image).where(Image.id.in_(image_ids)))
    print(f"  - 기존 데모 데이터 삭제: 유저 {len(user_ids)}명 · 게시글 {len(post_ids)}건")


# ---------------------------------------------------------------------------
# 생성
# ---------------------------------------------------------------------------
async def create_users(db: AsyncSession, uploader: ImageUploader, rng: random.Random) -> list[User]:
    now = datetime.now(UTC)
    # 전원 같은 비밀번호 — bcrypt를 계정 수만큼 돌릴 이유가 없다.
    hashed = await hash_password(password_with_pepper(DEMO_PASSWORD))
    users: list[User] = []

    for index, spec in enumerate(DEMO_USERS):
        user = User(
            email=spec["email"],
            password=hashed,
            nickname=spec["nickname"],
            created_at=now - timedelta(days=90 - index),
            updated_at=now,
        )
        db.add(user)
        await db.flush()
        image = await uploader.create(
            db,
            purpose="profile",
            palette_index=index,
            size=(240, 240),
            uploader_id=user.id,
            now=now,
        )
        if image is not None:
            user.profile_image_id = image.id
        users.append(user)

    for index, (name, breed, gender) in enumerate(DEMO_DOGS):
        owner = users[index % len(users)]
        image = await uploader.create(
            db,
            purpose="profile",
            palette_index=index + 2,
            size=(320, 320),
            uploader_id=owner.id,
            now=now,
        )
        db.add(
            DogProfile(
                owner_id=owner.id,
                name=name,
                breed=breed,
                gender=gender,
                birth_date=(now - timedelta(days=rng.randint(400, 2500))).date(),
                profile_image_id=image.id if image is not None else None,
                # 소유자당 대표견 1마리(부분 유니크 인덱스) — 첫 바퀴에서만 대표로 둔다.
                is_representative=index < len(users),
                created_at=now,
                updated_at=now,
            )
        )

    await db.flush()
    print(f"  - 유저 {len(users)}명 · 강아지 {len(DEMO_DOGS)}마리")
    return users


async def ensure_hashtags(db: AsyncSession) -> dict[str, int]:
    rows = await db.execute(select(Hashtag.name, Hashtag.id).where(Hashtag.name.in_(HASHTAGS)))
    tag_ids: dict[str, int] = {name: tag_id for name, tag_id in rows.all()}
    for name in HASHTAGS:
        if name in tag_ids:
            continue
        tag = Hashtag(name=name)
        db.add(tag)
        await db.flush()
        tag_ids[name] = tag.id
    return tag_ids


async def create_posts(
    db: AsyncSession,
    users: list[User],
    uploader: ImageUploader,
    rng: random.Random,
    count: int,
) -> list[Post]:
    tag_ids = await ensure_hashtags(db)
    dog_names = [d[0] for d in DEMO_DOGS]
    posts: list[Post] = []

    for index, created in enumerate(post_timestamps(count, rng)):
        category_id, title_tpl, body_tpl = POST_TEMPLATES[index % len(POST_TEMPLATES)]
        dog = rng.choice(dog_names)
        author = users[index % len(users)]
        post = Post(
            user_id=author.id,
            title=title_tpl.format(dog=dog),
            content=body_tpl.format(dog=dog) + "\n\n" + rng.choice(POST_TAILS),
            category_id=category_id,
            # 조회수는 write-behind 버퍼(Redis→DB)가 채우는 값의 자리. 분포를 넓게 둬야
            # 트렌딩 점수가 서로 갈린다.
            view_count=rng.randint(3, 40) * rng.choice([1, 1, 1, 5, 25]),
            created_at=created,
            updated_at=created,
        )
        db.add(post)
        await db.flush()

        for name in rng.sample(HASHTAGS, rng.randint(2, 4)):
            await db.execute(
                post_hashtags.insert().values(post_id=post.id, hashtag_id=tag_ids[name])
            )

        if index % 3 == 0:
            image = await uploader.create(
                db,
                purpose="post",
                palette_index=index,
                size=(800, 600),
                uploader_id=author.id,
                now=created,
            )
            if image is not None:
                db.add(PostImage(post_id=post.id, image_id=image.id, created_at=created))
        posts.append(post)

    await db.flush()
    print(f"  - 게시글 {len(posts)}건 (최근 24시간 내 {max(1, count * 3 // 10)}건)")
    return posts


async def create_engagement(
    db: AsyncSession,
    users: list[User],
    posts: list[Post],
    rng: random.Random,
) -> None:
    """댓글·대댓글·좋아요와 거기서 파생되는 알림. 비정규화 카운트도 함께 맞춘다."""
    totals = {"comment": 0, "reply": 0, "like": 0, "comment_like": 0, "notification": 0}

    for post in posts:
        author_id = post.user_id
        roots: list[Comment] = []
        reply_count = 0

        # 댓글도 게시글과 같은 규칙을 따른다(모듈 상단 1번) — 삽입 순서대로 시각이 증가해야
        # uuid7 id 순서와 표시되는 작성일이 일치한다. 글마다 독립 난수를 뿌리면 목록이
        # "최신순"인데 날짜는 뒤죽박죽으로 보인다(인기순의 동점 타이브레이커도 id다).
        at = post.created_at
        for _ in range(rng.randint(0, 4)):
            commenter = rng.choice(users)
            at = at + timedelta(minutes=rng.randint(5, 600))
            comment = Comment(
                post_id=post.id,
                author_id=commenter.id,
                content=rng.choice(COMMENT_TEMPLATES),
                created_at=at,
                updated_at=at,
            )
            db.add(comment)
            await db.flush()
            roots.append(comment)
            totals["comment"] += 1

            if author_id is not None and commenter.id != author_id:
                db.add(
                    Notification(
                        user_id=author_id,
                        kind=NotificationKind.COMMENT_ON_POST.value,
                        actor_id=commenter.id,
                        post_id=post.id,
                        comment_id=comment.id,
                        # 1/3은 안 읽은 채로 남겨 알림 배지가 보이게 한다.
                        read_at=None if rng.random() < 0.34 else at,
                        created_at=at,
                    )
                )
                totals["notification"] += 1

        if roots and rng.random() < 0.4:
            parent = roots[-1]
            at = parent.created_at + timedelta(minutes=rng.randint(3, 120))
            db.add(
                Comment(
                    post_id=post.id,
                    author_id=author_id,
                    parent_id=parent.id,
                    content=rng.choice(REPLY_TEMPLATES),
                    created_at=at,
                    updated_at=at,
                )
            )
            reply_count = 1
            totals["reply"] += 1

        # 댓글 좋아요 — 댓글 목록의 **기본 정렬이 인기순**(ADR 0016)이라 이게 없으면 데모에서
        # 인기순과 최신순이 똑같아 보인다. 전부 고르게 뿌리면 그것대로 순위가 안 드러나므로,
        # 루트의 절반 정도만 좋아요를 받게 두어 "반응받은 댓글이 위로" 가 눈에 보이게 한다.
        for root in roots:
            if rng.random() < 0.5:
                continue
            comment_likers = rng.sample(users, rng.randint(1, min(5, len(users))))
            for liker in comment_likers:
                at = root.created_at + timedelta(minutes=rng.randint(1, 480))
                db.add(CommentLike(comment_id=root.id, user_id=liker.id, created_at=at))
                totals["comment_like"] += 1
                if root.author_id is not None and liker.id != root.author_id and rng.random() < 0.3:
                    db.add(
                        Notification(
                            user_id=root.author_id,
                            kind=NotificationKind.LIKE_COMMENT.value,
                            actor_id=liker.id,
                            post_id=post.id,
                            comment_id=root.id,
                            read_at=None if rng.random() < 0.34 else at,
                            created_at=at,
                        )
                    )
                    totals["notification"] += 1
            root.like_count = len(comment_likers)

        likers = rng.sample(users, rng.randint(0, min(6, len(users))))
        for liker in likers:
            at = post.created_at + timedelta(minutes=rng.randint(1, 900))
            db.add(PostLike(post_id=post.id, user_id=liker.id, created_at=at))
            totals["like"] += 1
            if author_id is not None and liker.id != author_id and rng.random() < 0.3:
                db.add(
                    Notification(
                        user_id=author_id,
                        kind=NotificationKind.LIKE_POST.value,
                        actor_id=liker.id,
                        post_id=post.id,
                        comment_id=None,
                        read_at=None if rng.random() < 0.34 else at,
                        created_at=at,
                    )
                )
                totals["notification"] += 1

        post.like_count = len(likers)
        post.comment_count = len(roots) + reply_count

    await db.flush()
    print(
        f"  - 댓글 {totals['comment']}건 · 대댓글 {totals['reply']}건 · "
        f"좋아요 {totals['like']}건(댓글 {totals['comment_like']}건) · "
        f"알림 {totals['notification']}건"
    )


async def create_chats(db: AsyncSession, users: list[User], rng: random.Random) -> None:
    """데모 계정끼리 주고받은 DM. 첫 번째 계정이 두 방에 참여해 로그인하면 바로 보인다."""
    now = datetime.now(UTC)
    pairs = [(users[0], users[1]), (users[0], users[2]), (users[3], users[4])]

    for room_index, (a, b) in enumerate(pairs):
        user1_id, user2_id = normalize_dm_user_ids(a.id, b.id)
        started = now - timedelta(days=room_index + 1, hours=rng.randint(1, 20))
        room = ChatRoom(
            user1_id=user1_id, user2_id=user2_id, created_at=started, updated_at=started
        )
        db.add(room)
        await db.flush()
        speakers = (a, b)
        for msg_index, (speaker_index, text) in enumerate(DM_SCRIPT):
            at = started + timedelta(minutes=msg_index * rng.randint(2, 9))
            db.add(
                ChatMessage(
                    room_id=room.id,
                    sender_id=speakers[speaker_index].id,
                    content=text,
                    # 마지막 두 개는 안 읽음 — 방 목록의 미읽음 배지를 보여주기 위함.
                    is_read=msg_index < len(DM_SCRIPT) - 2,
                    created_at=at,
                )
            )

    await db.flush()
    print(f"  - 채팅방 {len(pairs)}개 · 메시지 {len(pairs) * len(DM_SCRIPT)}건")


# ---------------------------------------------------------------------------
async def seed(*, force: bool, post_count: int, with_images: bool) -> int:
    rng = random.Random(20260804)  # 고정 시드 — 다시 돌려도 같은 데이터가 나온다
    uploader = ImageUploader(enabled=with_images and bool(settings.S3_BUCKET_NAME))

    async with AsyncSessionLocal() as db, db.begin():
        emails = [u["email"] for u in DEMO_USERS]
        exists = (await db.execute(select(User.id).where(User.email.in_(emails)).limit(1))).first()
        if exists and not force:
            print("데모 데이터가 이미 있습니다. 다시 만들려면 --force 를 쓰세요.")
            return 0
        if force:
            await purge_demo_data(db)

        print("데모 데이터 생성 중…")
        users = await create_users(db, uploader, rng)
        posts = await create_posts(db, users, uploader, rng, post_count)
        await create_engagement(db, users, posts, rng)
        await create_chats(db, users, rng)

    # 비밀번호는 찍지 않는다. 값 자체는 로그인 화면에 공개되지만, 이름이 password인 것을
    # stdout에 흘리면 CI 로그·배포 셸 히스토리에 그대로 남고 정적 분석에도 걸린다
    # (CodeQL py/clear-text-logging-sensitive-data). 값은 README와 프론트 config.ts에 있다.
    print("\n완료. 데모 계정으로 로그인해 보세요:")
    print(f"  이메일   {DEMO_USERS[0]['email']}  (비밀번호는 README 참고)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="PuppyTalk 데모 데이터 시드")
    parser.add_argument(
        "--force", action="store_true", help="기존 데모 데이터를 지우고 다시 만든다"
    )
    parser.add_argument("--posts", type=int, default=120, help="생성할 게시글 수 (기본 120)")
    parser.add_argument("--no-images", action="store_true", help="이미지 업로드를 건너뛴다")
    args = parser.parse_args()
    return asyncio.run(
        seed(force=args.force, post_count=args.posts, with_images=not args.no_images)
    )


if __name__ == "__main__":
    raise SystemExit(main())
