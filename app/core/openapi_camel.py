# OpenAPI 스키마의 components.schemas 내 property 키를 camelCase로 변환.
# 실제 응답(serialize_by_alias)과 스펙을 일치시켜 프론트 codegen 시 변환 스크립트 불필요하게 함.

from typing import Any

# 응답 직렬화(BaseSchema alias_generator)와 같은 함수를 써야 스펙과 실응답 키가 어긋나지 않는다.
from app.common.schemas import to_camel


def _camel_key(name: str) -> str:
    """snake_case 키만 변환한다. BaseSchema 스키마는 alias로 이미 camelCase라, 밑줄 없는
    키에 to_camel을 또 태우면 'hasMore'→'hasmore'로 뭉개져 스펙이 실응답과 어긋난다."""
    return to_camel(name) if "_" in name else name


def _convert_schema_object(obj: Any) -> Any:
    """스키마 객체 내 'properties' 키를 camelCase로 변환. $ref는 유지.

    items·allOf·oneOf·anyOf 등 중첩 컨테이너는 아래 else의 재귀가 dict·list를
    동일하게 처리하므로 별도 분기가 필요 없다.
    """
    if obj is None:
        return None
    if isinstance(obj, list):
        return [_convert_schema_object(item) for item in obj]
    if not isinstance(obj, dict):
        return obj
    if "$ref" in obj:
        return obj
    out: dict[str, Any] = {}
    for k, v in obj.items():
        if k == "required" and isinstance(v, list):
            # properties 키를 camelCase로 바꾸면 required 이름도 동일하게 맞춰야 스펙이 유효함
            out[k] = [_camel_key(item) if isinstance(item, str) else item for item in v]
        elif k == "properties" and isinstance(v, dict):
            out[k] = {_camel_key(key): _convert_schema_object(val) for key, val in v.items()}
        else:
            out[k] = _convert_schema_object(v)
    return out


def openapi_schema_to_camel(schema: dict[str, Any]) -> dict[str, Any]:
    """components.schemas 내 각 스키마의 properties 키를 camelCase로 변환한 새 스펙 반환."""
    result = dict(schema)
    components = result.get("components") or {}
    schemas = components.get("schemas") or {}
    if schemas:
        converted = {name: _convert_schema_object(s) for name, s in schemas.items()}
        result["components"] = {**components, "schemas": converted}
    return result
