"""OpenAPI 스펙 camel 변환 — snake_case만 변환하고 이미 camelCase인 키는 보존하는지."""

from app.core.openapi_camel import openapi_schema_to_camel


def test_snake_converted_and_already_camel_preserved():
    """BaseSchema 스키마는 alias로 이미 camelCase — 이중 변환이 'hasMore'를 'hasmore'로
    뭉개던 회귀를 고정한다(프론트 codegen 타입이 실응답 키와 어긋나는 원인)."""
    spec = {
        "components": {
            "schemas": {
                "X": {
                    "properties": {
                        "has_more": {"type": "boolean"},
                        "requestId": {"type": "string"},
                        "email": {"type": "string"},
                    },
                    "required": ["has_more", "requestId"],
                }
            }
        }
    }
    out = openapi_schema_to_camel(spec)
    converted = out["components"]["schemas"]["X"]
    assert set(converted["properties"]) == {"hasMore", "requestId", "email"}
    assert converted["required"] == ["hasMore", "requestId"]
