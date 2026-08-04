# API 의존성 단일 진입점. 라우터/핸들러에서는 여기서만 import.
from .auth import (
    CurrentUser,
    get_current_admin,
    get_current_user,
    get_current_user_optional,
)
from .client import get_client_identifier
from .db import get_master_db, get_slave_db
from .permissions import (
    CommentAuthorContext,
    require_comment_author,
    require_comment_author_for_delete,
    require_post_author,
)
from .query import parse_availability_query
from .redis_client import get_optional_redis

__all__ = [
    "CommentAuthorContext",
    "CurrentUser",
    "get_client_identifier",
    "get_current_admin",
    "get_current_user",
    "get_current_user_optional",
    "get_master_db",
    "get_slave_db",
    "get_optional_redis",
    "parse_availability_query",
    "require_comment_author",
    "require_comment_author_for_delete",
    "require_post_author",
]
