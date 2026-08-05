"""관리자 권한 부여·회수 CLI.

`users.role`은 API로 바꿀 수 없다 — 권한 상승 엔드포인트를 두지 않는 게 설계 의도다.
그렇다고 psql로 UPDATE만 하면 **틀린다**: role은 Redis 인증 스냅샷(`user:auth:{id}`,
TTL 240초)에 함께 실려서, 무효화하지 않으면 최대 4분간 예전 권한으로 판정된다
(재로그인해도 캐시가 먼저 맞는다). 그 두 단계를 한 명령으로 묶고, 조회·판정에
앱과 같은 리포지토리·무효화 함수를 그대로 쓴다.

사용:
    uv run python -m app.scripts.grant_admin --email me@example.com
    uv run python -m app.scripts.grant_admin --email me@example.com --revoke
    uv run python -m app.scripts.grant_admin --list

종료 코드: 0 성공(변경 없음 포함) / 1 대상 없음 / 2 인자 오류(argparse).
"""

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.db.engine import AsyncSessionLocal
from app.db.statements import update_one_returning
from app.domain.auth.user_status_cache import invalidate_user_status_cache
from app.domain.users.model import User, UsersRepository
from app.infra.redis import RedisLike, create_redis_client

ROLE_ADMIN = "ADMIN"
ROLE_USER = "USER"


async def _open_redis() -> RedisLike | None:
    """앱 lifespan 밖에서 쓰는 단발 클라이언트. Redis가 없으면 캐시도 없으므로 None."""
    client = create_redis_client()
    if client is not None:
        await client.ping()
    return client


async def _list_admins() -> int:
    async with AsyncSessionLocal() as db, db.begin():
        rows = (
            await db.execute(
                select(User.email, User.nickname, User.status)
                .where(User.role == ROLE_ADMIN, User.deleted_at.is_(None))
                .order_by(User.created_at)
            )
        ).all()
    if not rows:
        print("관리자 계정이 없습니다.")
        return 0
    print(f"관리자 {len(rows)}명:")
    for email, nickname, status in rows:
        print(f"  - {email} ({nickname}) status={status}")
    return 0


async def _set_role(email: str, role: str) -> int:
    async with AsyncSessionLocal() as db:
        async with db.begin():
            user = await UsersRepository.get_user_by_email(email, db)
            if user is None:
                print(f"대상 없음: {email} (탈퇴 계정도 조회되지 않습니다)", file=sys.stderr)
                return 1
            user_id, previous = user.id, user.role
            if previous == role:
                print(f"이미 {role}입니다: {email}")
            else:
                await update_one_returning(db, User, [User.id == user_id], {"role": role}, User.id)
                print(f"{email}: {previous} → {role}")

    # 캐시 무효화는 트랜잭션 밖에서. 커밋되지 않은 변경으로 캐시를 지우면
    # 다음 요청이 예전 값을 다시 캐싱해 오히려 어긋난다.
    redis = await _open_redis()
    if redis is None:
        print("Redis 미설정 — 인증 스냅샷 캐시가 없으므로 즉시 반영됩니다.")
        return 0
    try:
        await invalidate_user_status_cache(redis, user_id)
        print("인증 스냅샷 캐시 무효화 완료 — 다음 요청부터 반영됩니다.")
    finally:
        await redis.aclose()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="grant_admin",
        description="users.role을 ADMIN/USER로 바꾸고 인증 스냅샷 캐시를 무효화한다.",
    )
    parser.add_argument("--email", help="대상 계정 이메일")
    parser.add_argument("--revoke", action="store_true", help="ADMIN을 회수하고 USER로 되돌린다")
    parser.add_argument("--list", action="store_true", help="현재 관리자 목록만 출력")
    args = parser.parse_args()

    if args.list:
        return asyncio.run(_list_admins())
    if not args.email:
        parser.error("--email 또는 --list 중 하나가 필요합니다.")
    return asyncio.run(_set_role(args.email, ROLE_USER if args.revoke else ROLE_ADMIN))


if __name__ == "__main__":
    raise SystemExit(main())
