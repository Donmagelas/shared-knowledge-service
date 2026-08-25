"""在 OGX v1.3.0 标量 attributes 限制下保留公开数组属性。"""

from __future__ import annotations

import json
from typing import Any

ARRAY_PREFIX = "__shared_knowledge_array_v1__:"
type AttributeScalar = str | int | float | bool
type AttributeValue = AttributeScalar | list[AttributeScalar]


def encode_attributes_for_ogx(attributes: dict[str, AttributeValue]) -> dict[str, str | int | float | bool]:
    """把一维标量数组无损编码为 OGX 允许的字符串值。"""

    encoded: dict[str, str | int | float | bool] = {}
    for key, value in attributes.items():
        if not isinstance(value, list):
            encoded[key] = value
            continue
        serialized = f"{ARRAY_PREFIX}{json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
        if len(serialized) > 512:
            raise ValueError(f"attributes.{key} 编码后超过 OGX 512 字符限制")
        encoded[key] = serialized
    return encoded


def decode_attribute_from_ogx(value: Any) -> Any:
    """只解码本服务带版本前缀的数组，不碰普通业务字符串。"""

    if not isinstance(value, str) or not value.startswith(ARRAY_PREFIX):
        return value
    try:
        decoded = json.loads(value[len(ARRAY_PREFIX) :])
    except json.JSONDecodeError as exc:
        raise ValueError("OGX 中的数组属性编码已损坏") from exc
    if not isinstance(decoded, list):
        raise ValueError("OGX 中的数组属性编码不是列表")
    return decoded
