# 1:1 DM 비즈니스 로직. 방 upsert·메시지 저장·Redis 팬아웃·커서 목록.

import asyncio
import json
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, case, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.common import split_page
from app.common.enums import UserStatus
from app.common.exceptions import (
    ForbiddenException,
    InvalidRequestException,
    SelfDmException,
    UserNotFoundException,
)
from app.core.ids import new_uuid7
from app.db.base_class import utc_now
from app.domain.chat.model import ChatMessage, ChatRoom, normalize_dm_user_ids
from app.domain.chat.schema import (
    ChatMessageBroadcast,
    ChatMessageItem,
    ChatMessageSend,
    ChatRoomListItem,
    ChatRoomPeerInfoData,
    ChatRoomsListData,
)
from app.domain.dogs.model import DogProfile
from app.domain.media.model import Image
from app.domain.users.model import User, UsersModel
from app.infra.pubsub import publish_user_envelope
from app.infra.redis import RedisLike

from .manager import CHAT_DM_FANOUT_CHANNEL, chat_connection_manager


class _PeerJoin:
    """방 목록·방 상단 정보가 공유하는 상대(peer) 표시 정보 한 벌.

    peer 판별 CASE 식, alias 4개, 조인 4단, 결과 행 → 스키마 필드 매핑까지 같은 골격이라
    여기 한 곳에서 소유한다 — peer 표시 컬럼이 늘면 columns·fields만 고친다.
    """

    def __init__(self, user_id: UUID) -> None:
        self.peer_id = case(
            (ChatRoom.user1_id == user_id, ChatRoom.user2_id),
            else_=ChatRoom.user1_id,
        ).label("peer_id")
        self._peer = aliased(User)
        self._peer_img = aliased(Image)
        self._peer_dog = aliased(DogProfile)
        self._peer_dog_img = aliased(Image)
        self.columns = (
            self.peer_id,
            self._peer.nickname.label("peer_nickname"),
            self._peer_img.file_url.label("peer_profile_image_url"),
            self._peer_dog_img.file_url.label("peer_dog_profile_image_url"),
            self._peer_dog.name.label("peer_dog_name"),
            self._peer_dog.breed.label("peer_dog_breed"),
            self._peer_dog.gender.label("peer_dog_gender"),
            self._peer_dog.birth_date.label("peer_dog_birth_date"),
        )

    def apply(self, stmt: Select[Any]) -> Select[Any]:
        return (
            stmt.join(self._peer, self._peer.id == self.peer_id)
            .outerjoin(self._peer_img, self._peer_img.id == self._peer.profile_image_id)
            .outerjoin(
                self._peer_dog,
                (self._peer_dog.owner_id == self._peer.id)
                & (self._peer_dog.is_representative.is_(True)),
            )
            .outerjoin(self._peer_dog_img, self._peer_dog_img.id == self._peer_dog.profile_image_id)
        )

    @staticmethod
    def fields(row: Any) -> dict[str, Any]:
        """SELECT 라벨 → ChatPeerProfile 필드 kwargs."""
        return {
            "peer_user_id": row.peer_id,
            "peer_nickname": row.peer_nickname or "",
            "peer_profile_image_url": row.peer_profile_image_url,
            "peer_dog_profile_image_url": row.peer_dog_profile_image_url,
            "peer_dog_name": row.peer_dog_name,
            "peer_dog_breed": row.peer_dog_breed,
            "peer_dog_gender": row.peer_dog_gender,
            "peer_dog_birth_date": row.peer_dog_birth_date,
        }


class ChatService:
    @classmethod
    async def resolve_direct_room(
        cls,
        db: AsyncSession,
        *,
        user_id: UUID,
        peer_id: UUID,
    ) -> UUID:
        """1:1 방 조회·없으면 생성 후 room id 반환."""
        async with db.begin():
            return await cls.get_or_create_room(db, user_id=user_id, peer_id=peer_id)

    @classmethod
    async def get_or_create_room(
        cls,
        db: AsyncSession,
        *,
        user_id: UUID,
        peer_id: UUID,
    ) -> UUID:
        """방 upsert 후 room id 반환.

        자기 자신·상대 존재/활성·차단 검사를 전부 이 지점에 둔다 — WS 전송·REST 방 열기 등
        어떤 진입점이든 여기를 지나므로 경로별로 검사를 기억할 필요가 없다. 차단 문구는
        누가 차단했는지 노출하지 않는 중립 표현.
        """
        if peer_id == user_id:
            raise SelfDmException()
        peer = await UsersModel.get_status_and_block_between(user_id, peer_id, db=db)
        if peer is None or not UserStatus.is_active_value(peer.status):
            raise UserNotFoundException(message="상대방을 찾을 수 없습니다.")
        if peer.blocked:
            raise ForbiddenException(message="메시지를 보낼 수 없는 상대입니다.")
        u1, u2 = normalize_dm_user_ids(user_id, peer_id)
        now = utc_now()
        res = await db.execute(
            pg_insert(ChatRoom)
            .values(id=new_uuid7(), user1_id=u1, user2_id=u2, created_at=now, updated_at=now)
            # DO NOTHING은 충돌(기존 방) 경로에서 RETURNING이 비므로, 기존 행의 updated_at을
            # 갱신하는 DO UPDATE로 두 경로 모두 한 문장에서 id가 돌아오게 한다.
            .on_conflict_do_update(constraint="uq_chat_rooms_user_pair", set_={"updated_at": now})
            .returning(ChatRoom.id)
        )
        return res.scalar_one()

    @classmethod
    async def send_dm_from_ws(
        cls,
        db: AsyncSession,
        *,
        sender_id: UUID,
        payload: ChatMessageSend,
        redis: RedisLike | None,
    ) -> None:
        peer_id = payload.peer_user_id
        async with db.begin():
            # 자기 자신·상대·차단 검사는 get_or_create_room 안에서 수행된다(모든 진입점 공통).
            room_id = await cls.get_or_create_room(db, user_id=sender_id, peer_id=peer_id)
            msg = ChatMessage(
                id=new_uuid7(),
                room_id=room_id,
                sender_id=sender_id,
                content=payload.content,
                is_read=False,
                created_at=utc_now(),
            )
            db.add(msg)
            await db.flush()
            wire = json.dumps(
                ChatMessageBroadcast.model_validate(msg).model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
            )
        await cls._fanout_dm(redis, peer_id=peer_id, sender_id=sender_id, wire=wire)

    @classmethod
    async def _fanout_dm(
        cls,
        redis: RedisLike | None,
        *,
        peer_id: UUID,
        sender_id: UUID,
        wire: str,
    ) -> None:
        # 로컬 소켓 직접 전달과 크로스 인스턴스 envelope 발행은 서로 의존이 없고 각자
        # 실패를 삼키므로 병렬로 — 정체 소켓·Redis 지연이 합산되지 않는다.
        # (publish 성공이 로컬 전달을 보장하지 않는다: 소비는 별도 리스너 연결 몫이라
        # 리스너 재연결 창에서는 성공한 publish도 로컬에 도달하지 않는다. 리스너가 origin
        # 비교로 자기 발행분을 건너뛰므로 중복 전달 없음. publish 실패는 다른 인스턴스
        # 수신자만 유실 — at-most-once.)
        targets = [peer_id, sender_id]
        await asyncio.gather(
            *(chat_connection_manager.send_personal_message(uid, wire) for uid in targets),
            publish_user_envelope(
                redis, CHAT_DM_FANOUT_CHANNEL, target_user_ids=targets, payload=wire
            ),
        )

    @classmethod
    async def _room_membership_guard(
        cls,
        db: AsyncSession,
        *,
        room_id: UUID,
        user_id: UUID,
        cursor_message_id: UUID | None = None,
    ) -> tuple[Any, Any] | None:
        """메시지 조회·UPDATE 앞의 authz 가드. 멤버 판정에 필요한 두 컬럼만 로드하며,
        방이 없거나 멤버가 아니면 Forbidden.

        cursor_message_id가 주어지면 커서 행 로드를 같은 문장에 접어(outerjoin) 별도
        왕복을 없애고 (created_at, id)를 반환한다 — 방에 없는 커서는 InvalidRequest.
        """
        stmt: Select[Any] = select(ChatRoom.user1_id, ChatRoom.user2_id)
        if cursor_message_id is not None:
            stmt = select(
                ChatRoom.user1_id, ChatRoom.user2_id, ChatMessage.created_at, ChatMessage.id
            ).outerjoin(
                ChatMessage,
                and_(ChatMessage.id == cursor_message_id, ChatMessage.room_id == room_id),
            )
        stmt = stmt.select_from(ChatRoom).where(ChatRoom.id == room_id).limit(1)
        row = (await db.execute(stmt)).one_or_none()
        if row is None or user_id not in (row.user1_id, row.user2_id):
            raise ForbiddenException(message="이 채팅방에 접근할 수 없습니다.")
        if cursor_message_id is None:
            return None
        if row.created_at is None:
            raise InvalidRequestException(message="유효하지 않은 cursor 입니다.")
        return row.created_at, row.id

    @classmethod
    async def list_room_messages(
        cls,
        db: AsyncSession,
        *,
        room_id: UUID,
        user_id: UUID,
        cursor_message_id: UUID | None,
        limit: int,
    ) -> tuple[list[ChatMessageItem], bool]:
        async with db.begin():
            cursor_row = await cls._room_membership_guard(
                db, room_id=room_id, user_id=user_id, cursor_message_id=cursor_message_id
            )
            stmt = select(ChatMessage).where(ChatMessage.room_id == room_id)
            if cursor_row is not None:
                stmt = stmt.where(
                    tuple_(ChatMessage.created_at, ChatMessage.id) < tuple_(*cursor_row)
                )
            stmt = stmt.order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(
                limit + 1
            )
            mres = await db.execute(stmt)
            rows = list(mres.scalars().all())
        page_rows, has_more = split_page(rows, limit)
        return [ChatMessageItem.model_validate(m) for m in page_rows], has_more

    @classmethod
    async def list_recent_rooms(
        cls,
        db: AsyncSession,
        *,
        user_id: UUID,
        limit: int,
    ) -> ChatRoomsListData:
        """최근 대화 목록(헤더 인박스용).

        - 방은 "최근 메시지가 존재"하는 경우만 노출(빈 방 제외)
        - 미읽음은 내 기준: is_read=false AND sender_id != me
        - N+1 방지: 방/상대/최근메시지/미읽음을 1회 쿼리로 조립
        """
        pj = _PeerJoin(user_id)

        # 두 서브쿼리(최근메시지 윈도우·미읽음 집계)를 호출자 방으로 한정한다.
        # 스코프가 없으면 전체 chat_messages를 스캔·집계해 메시지 누적에 비례해 비용이 커진다(#16).
        my_room_ids = (
            select(ChatRoom.id)
            .where(or_(ChatRoom.user1_id == user_id, ChatRoom.user2_id == user_id))
            .scalar_subquery()
        )

        last_msg_ranked = (
            select(
                ChatMessage.room_id.label("room_id"),
                ChatMessage.content.label("last_content"),
                ChatMessage.created_at.label("last_created_at"),
                func.row_number()
                .over(
                    partition_by=ChatMessage.room_id,
                    order_by=(ChatMessage.created_at.desc(), ChatMessage.id.desc()),
                )
                .label("rn"),
            )
            .where(ChatMessage.room_id.in_(my_room_ids))
            .subquery()
        )
        last_msg = (
            select(
                last_msg_ranked.c.room_id,
                last_msg_ranked.c.last_content,
                last_msg_ranked.c.last_created_at,
            )
            .where(last_msg_ranked.c.rn == 1)
            .subquery()
        )

        unread = (
            select(
                ChatMessage.room_id.label("room_id"),
                func.count(ChatMessage.id).label("unread_count"),
            )
            .where(
                ChatMessage.room_id.in_(my_room_ids),
                ChatMessage.is_read.is_(False),
                ChatMessage.sender_id != user_id,
            )
            .group_by(ChatMessage.room_id)
            .subquery()
        )

        async with db.begin():
            stmt = select(
                ChatRoom.id.label("room_id"),
                *pj.columns,
                last_msg.c.last_content,
                last_msg.c.last_created_at,
                func.coalesce(unread.c.unread_count, 0).label("unread_count"),
            ).where(or_(ChatRoom.user1_id == user_id, ChatRoom.user2_id == user_id))
            stmt = (
                pj.apply(stmt)
                .join(last_msg, last_msg.c.room_id == ChatRoom.id)
                .outerjoin(unread, unread.c.room_id == ChatRoom.id)
                .order_by(last_msg.c.last_created_at.desc(), ChatRoom.id.desc())
                .limit(limit)
            )
            res = await db.execute(stmt)
            rows = res.all()

        items: list[ChatRoomListItem] = []
        for r in rows:
            preview = (r.last_content or "").replace("\n", " ").strip()
            if len(preview) > 120:
                preview = preview[:117] + "…"
            items.append(
                ChatRoomListItem(
                    room_id=r.room_id,
                    last_message_preview=preview,
                    unread_count=int(r.unread_count or 0),
                    updated_at=r.last_created_at,
                    **_PeerJoin.fields(r),
                )
            )
        return ChatRoomsListData(items=items)

    @classmethod
    async def mark_room_read(
        cls,
        db: AsyncSession,
        *,
        room_id: UUID,
        user_id: UUID,
    ) -> None:
        """내 기준 미읽음(상대가 보낸 메시지)을 읽음으로 일괄 표시."""
        async with db.begin():
            await cls._room_membership_guard(db, room_id=room_id, user_id=user_id)
            await db.execute(
                update(ChatMessage)
                .where(
                    ChatMessage.room_id == room_id,
                    ChatMessage.sender_id != user_id,
                    ChatMessage.is_read.is_(False),
                )
                .values(is_read=True)
            )

    @classmethod
    async def get_room_peer_info(
        cls,
        db: AsyncSession,
        *,
        room_id: UUID,
        user_id: UUID,
    ) -> ChatRoomPeerInfoData:
        """채팅방 상단용 상대 정보(닉네임/프로필).

        - 멤버가 아니면 Forbidden
        - N+1 없이 1쿼리
        """
        pj = _PeerJoin(user_id)

        async with db.begin():
            # 멤버십 가드를 projection의 WHERE에 접어넣어 같은 방을 두 번 조회하지 않는다(#19).
            # 비멤버·부재 방이면 행이 없으므로 None → Forbidden.
            stmt = select(ChatRoom.id.label("room_id"), *pj.columns).where(
                ChatRoom.id == room_id,
                or_(ChatRoom.user1_id == user_id, ChatRoom.user2_id == user_id),
            )
            stmt = pj.apply(stmt).limit(1)
            res = await db.execute(stmt)
            row = res.one_or_none()
            if row is None:
                raise ForbiddenException(message="이 채팅방에 접근할 수 없습니다.")

        return ChatRoomPeerInfoData(room_id=row.room_id, **_PeerJoin.fields(row))
