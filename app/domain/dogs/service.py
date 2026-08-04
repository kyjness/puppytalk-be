# 강아지 프로필 비즈니스 로직. 순수 데이터/커스텀 예외. Full-Async.

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import (
    ForbiddenException,
    InternalServerErrorException,
    NotFoundException,
)
from app.db import utc_now
from app.domain.dogs.model import DogProfilesRepository
from app.domain.dogs.schema import DogProfileUpsertItem
from app.domain.users.model import UsersRepository
from app.domain.users.schema import UserProfileResponse


class DogService:
    @classmethod
    async def upsert_dog_profile(
        cls,
        owner_id: UUID,
        items: Sequence[DogProfileUpsertItem | dict[str, object]],
        db: AsyncSession,
    ) -> None:
        """강아지 목록 전체 교체(생성/수정/삭제). 대표 강아지 설정은 한 트랜잭션 내 원자적.

        create/update 행은 is_representative를 항상 False로 넣고, 대표 배정은 마지막
        set_representative(전체 False→1개 True)에 일임한다. 부분 유니크 인덱스가 대표견 True를
        한 트랜잭션 안에서도 소유자당 1개로 강제하므로, 인라인으로 True를 여러 행에 넣으면
        statement 시점에 일시적 중복으로 거부된다.
        """
        existing_ids = await DogProfilesRepository.get_ids_by_owner_id(owner_id, db=db)
        update_ids: list[UUID] = []
        create_rows: list[dict[str, object]] = []
        update_rows: list[dict[str, object]] = []
        representative_existing_id: UUID | None = None
        representative_new_index: int | None = None

        for raw in items:
            item: DogProfileUpsertItem = DogProfileUpsertItem.model_validate(raw)
            touch_dog_image = "profile_image_id" in item.model_fields_set
            gender_value = str(getattr(item.gender, "value", item.gender))
            if item.id is None:
                create_rows.append(
                    {
                        "name": item.name,
                        "breed": item.breed,
                        "gender": gender_value,
                        "birth_date": item.birth_date,
                        "profile_image_id": item.profile_image_id,
                        "is_representative": False,  # 대표 배정은 set_representative가 전담
                    }
                )
                if item.is_representative:
                    representative_new_index = len(create_rows) - 1
                    representative_existing_id = None
            else:
                update_ids.append(item.id)
                update_rows.append(
                    {
                        "dog_id": item.id,
                        "name": item.name,
                        "breed": item.breed,
                        "gender": gender_value,
                        "birth_date": item.birth_date,
                        "profile_image_id": item.profile_image_id,
                        "touch_profile_image": touch_dog_image,
                        "is_representative": False,  # 대표 배정은 set_representative가 전담
                        "updated_at": utc_now(),
                    }
                )
                if item.is_representative:
                    representative_existing_id = item.id
                    representative_new_index = None

        owned_update_ids = await DogProfilesRepository.get_owned_ids_in(owner_id, update_ids, db=db)
        if len(owned_update_ids) != len(set(update_ids)):
            raise ForbiddenException()

        if update_rows:
            await DogProfilesRepository.bulk_update_by_owner(owner_id, update_rows, db=db)

        created = await DogProfilesRepository.create_many(owner_id, create_rows, db=db)

        requested_ids: set[UUID] = set(update_ids)
        requested_ids.update(d.id for d in created)

        delete_ids = list(existing_ids - requested_ids)
        if delete_ids:
            await DogProfilesRepository.bulk_delete_by_owner_ids(owner_id, delete_ids, db=db)

        representative_id: UUID | None = representative_existing_id
        if representative_new_index is not None:
            if 0 <= representative_new_index < len(created):
                representative_id = created[representative_new_index].id
            else:
                representative_id = None
        if representative_id and requested_ids:
            await DogProfilesRepository.set_representative(owner_id, representative_id, db=db)

    @classmethod
    async def set_representative_dog(
        cls, owner_id: UUID, dog_id: UUID, db: AsyncSession
    ) -> UserProfileResponse:
        """대표 강아지 설정. dog_id가 해당 owner_id 소유가 아니면 NotFoundException. 반환: 갱신된 사용자 프로필."""
        async with db.begin():
            dog = await DogProfilesRepository.get_by_id(dog_id, owner_id, db=db)
            if dog is None:
                raise NotFoundException()
            if not await DogProfilesRepository.set_representative(owner_id, dog_id, db=db):
                raise InternalServerErrorException()
            user = await UsersRepository.get_user_by_id_with_dogs(owner_id, db=db)
            if not user:
                raise InternalServerErrorException()
            return UserProfileResponse.model_validate(user)
