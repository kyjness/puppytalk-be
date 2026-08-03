"""대표견 전용 뷰 관계 + 부분 유니크 인덱스 (#11) — DB 없이 검증하는 회귀 테스트.

부분 컬렉션 트랩(User.dogs를 .and_() 필터로 로드하면 컬렉션이 truncate됨)을 막기 위해
대표견을 전용 relationship으로 옮긴 결정과, 소유자당 1마리 불변식의 DB 승격을 고정한다.
"""

import inspect

from app.domain.comments.repository import _comment_author_loads
from app.domain.dogs.model import DogProfile
from app.domain.posts.repository import _post_author_and_content_loads
from app.domain.users.model import User, author_display_loads
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import RelationshipProperty
from sqlalchemy.schema import CreateIndex


def test_representative_dog_is_viewonly_single_relationship():
    rel = sa_inspect(User).relationships["representative_dog"]
    assert isinstance(rel, RelationshipProperty)
    assert rel.uselist is False
    assert rel.viewonly is True


def test_representative_dog_primaryjoin_filters_is_representative():
    rel = sa_inspect(User).relationships["representative_dog"]
    predicate = str(rel.primaryjoin)
    assert "owner_id" in predicate
    assert "is_representative" in predicate


def test_representative_dog_property_removed():
    # relationship이 단일 출처가 되도록 옛 @property는 제거됐어야 한다(둘이 공존하면 매핑 충돌).
    assert not isinstance(vars(User).get("representative_dog"), property)


def test_owner_representative_partial_unique_index_ddl():
    table = DogProfile.metadata.tables["dog_profiles"]
    idx = next(i for i in table.indexes if i.name == "uq_dog_profiles_owner_representative")
    assert idx.unique is True
    ddl = str(CreateIndex(idx).compile(dialect=postgresql.dialect()))
    assert "UNIQUE INDEX" in ddl
    assert "(owner_id)" in ddl
    assert "WHERE is_representative" in ddl


def test_author_loaders_avoid_dogs_collection_trap():
    # 작성자 로드 정책의 단일 정의처(users.author_display_loads)가 대표견 전용 관계로
    # 로드하고, User.dogs를 .and_()로 필터-로드(컬렉션 truncate 트랩)하지 않는지 소스로
    # 고정한다. posts·comments 핫패스는 이 공용 로더를 경유해야 한다.
    src = inspect.getsource(author_display_loads)
    assert "User.representative_dog" in src
    assert "User.dogs" not in src
    for loader in (_post_author_and_content_loads, _comment_author_loads):
        loader_src = inspect.getsource(loader)
        assert "author_display_loads" in loader_src
        # 핫패스 로더가 공용 로더를 우회해 트랩을 다시 들이는 것도 막는다.
        assert "User.dogs" not in loader_src
