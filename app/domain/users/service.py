# 사용자 비즈니스 로직. 순수 데이터 반환·커스텀 예외. 프로필 이미지는 users.profile_image_id만 갱신(고아 정리는 Media sweeper). Full-Async.

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import (
    InternalServerErrorException,
    InvalidRequestException,
    NicknameAlreadyExistsException,
    UnauthorizedException,
)
from app.core.security import (
    hash_password,
    password_with_pepper,
    verify_password_with_legacy_fallback,
)
from app.domain.dogs.service import DogService
from app.domain.media.model import MediaModel
from app.domain.users.model import UsersModel
from app.domain.users.schema import (
    AvailabilityData,
    BlockedUserItem,
    BlocksData,
    UpdatePasswordRequest,
    UpdateUserRequest,
    UserAvailabilityQuery,
    UserProfileResponse,
)


class UserService:
    @classmethod
    async def check_availability(
        cls, query: UserAvailabilityQuery, db: AsyncSession
    ) -> AvailabilityData:
        async with db.begin():
            return AvailabilityData(
                email_available=not await UsersModel.email_exists(query.email, db=db)
                if query.email
                else None,
                nickname_available=not await UsersModel.nickname_exists(query.nickname, db=db)
                if query.nickname
                else None,
            )

    @classmethod
    async def get_user_profile(cls, user_id: UUID, db: AsyncSession) -> UserProfileResponse:
        async with db.begin():
            user_with_dogs = await UsersModel.get_user_by_id_with_dogs(user_id, db=db)
            if not user_with_dogs:
                raise UnauthorizedException()
            return UserProfileResponse.model_validate(user_with_dogs)

    @classmethod
    async def update_user_profile(
        cls,
        user_id: UUID,
        data: UpdateUserRequest,
        db: AsyncSession,
    ) -> UserProfileResponse:
        fields_set = data.model_fields_set

        async with db.begin():
            user = await UsersModel.get_user_by_id(user_id, db=db)
            if not user:
                raise UnauthorizedException()

            # 1) 검증 + 명시적 필드 매핑
            updates: dict = {}
            if data.nickname is not None:
                if data.nickname != user.nickname and await UsersModel.nickname_exists(
                    data.nickname, db=db
                ):
                    raise NicknameAlreadyExistsException()
                updates["nickname"] = data.nickname

            wants_profile_image_change = (
                "profile_image_id" in fields_set or data.clear_profile_image is True
            )
            if wants_profile_image_change:
                is_clear = data.clear_profile_image is True or (
                    "profile_image_id" in fields_set and data.profile_image_id is None
                )
                if is_clear:
                    updates["profile_image_id"] = None
                else:
                    new_pid = data.profile_image_id
                    if new_pid is None or await MediaModel.get_image_by_id(new_pid, db=db) is None:
                        raise InvalidRequestException()
                    updates["profile_image_id"] = new_pid

            # 2) 유저 행 갱신
            if updates and not await UsersModel.update_user(user_id, db=db, **updates):
                raise InternalServerErrorException()

            # 3) 강아지 프로필(동일 트랜잭션)
            if data.dogs is not None:
                await DogService.upsert_dog_profile(user_id, data.dogs, db=db)

            db.expire(user)
            user_updated = await UsersModel.get_user_by_id_with_dogs(user_id, db=db)
            if not user_updated:
                raise InternalServerErrorException()
            result = UserProfileResponse.model_validate(user_updated)

        return result

    @classmethod
    async def update_password(
        cls, user_id: UUID, data: UpdatePasswordRequest, db: AsyncSession
    ) -> None:
        async with db.begin():
            hashed = await UsersModel.get_password_hash(user_id, db=db)
            if not hashed or not await verify_password_with_legacy_fallback(
                data.current_password, hashed
            ):
                raise UnauthorizedException()
            if await verify_password_with_legacy_fallback(data.new_password, hashed):
                raise InvalidRequestException(
                    "기존 비밀번호와 동일한 비밀번호는 사용할 수 없습니다."
                )
            new_hashed = await hash_password(password_with_pepper(data.new_password))
            if not await UsersModel.update_password(user_id, new_hashed, db=db):
                raise InternalServerErrorException()

    @classmethod
    async def get_blocked_list(cls, blocker_id: UUID, db: AsyncSession) -> BlocksData:
        async with db.begin():
            users = await UsersModel.get_blocked_users(blocker_id, db=db)
        items = [
            BlockedUserItem(
                id=u.id,
                nickname=u.nickname,
                profile_image_url=u.profile_image_url,
            )
            for u in users
        ]
        return BlocksData(items=items)

    @classmethod
    async def toggle_block_user(
        cls, blocker_id: UUID, target_user_id: UUID, db: AsyncSession
    ) -> bool:
        """이미 차단되어 있으면 해제(delete), 아니면 차단(insert). 반환: 차단 여부(True=차단됨, False=해제됨)."""
        if blocker_id == target_user_id:
            raise InvalidRequestException("자기 자신은 차단할 수 없습니다.")
        async with db.begin():
            exists = await UsersModel.block_exists(blocker_id, target_user_id, db=db)
            if exists:
                await UsersModel.unblock_user(blocker_id, target_user_id, db=db)
                return False
            await UsersModel.block_user(blocker_id, target_user_id, db=db)
            return True

    @classmethod
    async def delete_user(cls, user_id: UUID, db: AsyncSession) -> None:
        async with db.begin():
            user = await UsersModel.get_user_by_id(user_id, db=db)
            if not user:
                raise InternalServerErrorException()
            if not await UsersModel.delete_user(user_id, db=db):
                raise InternalServerErrorException()

    @classmethod
    async def purge_withdrawn_users(cls, *, older_than_days: int, db: AsyncSession) -> int:
        """탈퇴 유저 하드 삭제(청크 반복)."""
        total = 0
        # 단일 트랜잭션에 너무 많이 태우면 락/부하가 커질 수 있어, 청크별 begin()으로 끊는다.
        while True:
            async with db.begin():
                deleted_ids = await UsersModel.purge_withdrawn_users_older_than(
                    older_than_days=older_than_days,
                    limit=200,
                    db=db,
                )
            if not deleted_ids:
                break
            total += len(deleted_ids)
        return total
