"""인증 스냅샷 캐시(user:auth) 단위 테스트 — CurrentUser 직렬화 왕복·fail-open·동시 무효화."""

import json
import uuid
from typing import Any, cast

import pytest
from app.api.dependencies.auth import CurrentUser
from app.domain.auth.user_status_cache import (
    get_auth_cache,
    invalidate_user_status_cache,
    set_auth_cache_best_effort,
    user_auth_cache_key,
    user_status_cache_key,
)

from tests.unit.fakes import FakeRedis

pytestmark = pytest.mark.asyncio


async def test_current_user_snapshot_roundtrip():
    """model_dump(json/by_alias) → 캐시 → model_validate가 PublicId(Base62↔UUID)를 보존."""
    uid = uuid.uuid4()
    user = CurrentUser(id=uid, email="a@b.c", nickname="닉네임", role="USER")
    redis = FakeRedis()
    await set_auth_cache_best_effort(
        cast(Any, redis),
        uid,
        status_value="ACTIVE",
        user_payload=user.model_dump(mode="json", by_alias=True),
    )
    cached = await get_auth_cache(cast(Any, redis), uid)
    assert cached is not None and cached["status"] == "ACTIVE"
    restored = CurrentUser.model_validate(cached["user"])
    assert restored.id == uid
    assert (restored.nickname, restored.email, restored.role) == ("닉네임", "a@b.c", "USER")
    assert restored.created_at == user.created_at


async def test_get_auth_cache_treats_garbage_as_miss():
    """비JSON·비dict 값(배포 중 구버전 등)은 예외 없이 미스로 취급한다."""
    redis = FakeRedis()
    uid = uuid.uuid4()
    await redis.set(user_auth_cache_key(uid), "not-json")
    assert await get_auth_cache(cast(Any, redis), uid) is None
    await redis.set(user_auth_cache_key(uid), json.dumps(["list"]))
    assert await get_auth_cache(cast(Any, redis), uid) is None


async def test_invalidate_deletes_status_and_auth_keys_together():
    redis = FakeRedis()
    uid = uuid.uuid4()
    await redis.set(user_status_cache_key(uid), "ACTIVE")
    await redis.set(user_auth_cache_key(uid), "{}")
    await invalidate_user_status_cache(cast(Any, redis), uid)
    assert await redis.get(user_status_cache_key(uid)) is None
    assert await redis.get(user_auth_cache_key(uid)) is None
