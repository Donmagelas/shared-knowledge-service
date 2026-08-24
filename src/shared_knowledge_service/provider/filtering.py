"""把 OGX 文件属性过滤条件安全翻译为 Qdrant Payload Filter。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from ogx_api import ComparisonFilter, CompoundFilter
from qdrant_client import models

from .config import PayloadIndexType

type QdrantCondition = (
    models.FieldCondition
    | models.IsEmptyCondition
    | models.IsNullCondition
    | models.HasIdCondition
    | models.HasVectorCondition
    | models.NestedCondition
    | models.Filter
)

# 这些字段由知识库服务生成，不能通过业务 attributes 伪造。
RESERVED_ATTRIBUTE_FIELDS = frozenset(
    {
        "attributes",
        "chunk_content",
        "chunk_id",
        "content_text",
        "vector_store_id",
    }
)

SYSTEM_FILTER_FIELDS = frozenset({"file_id", "chunk_id"})


class FilterTranslationError(ValueError):
    """表示调用方提交了无法安全执行的过滤条件。"""


def payload_field_path(key: str) -> str:
    """把公开属性名映射到 Qdrant Payload 路径。"""

    normalized = key.strip()
    if not normalized:
        raise FilterTranslationError("过滤字段不能为空")
    if normalized == "vector_store_id" or normalized.startswith("vector_store_id."):
        raise FilterTranslationError("vector_store_id 由知识库服务强制添加，不能由调用方过滤")
    if normalized in SYSTEM_FILTER_FIELDS:
        return normalized
    if normalized in RESERVED_ATTRIBUTE_FIELDS or normalized.startswith("attributes."):
        raise FilterTranslationError(f"不能直接访问保留 Payload 字段：{normalized}")
    return f"attributes.{normalized}"


def _require_sequence(value: object, operator: str) -> list[str] | list[int]:
    """校验 Qdrant MatchAny 支持的同类型字符串或整数列表。"""

    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise FilterTranslationError(f"{operator} 的值必须是字符串列表或整数列表")
    items = list(value)
    if not items:
        raise FilterTranslationError(f"{operator} 的值不能为空列表")
    if all(isinstance(item, str) for item in items):
        return items
    # bool 是 int 的子类，但 Qdrant MatchAny 不接受布尔列表。
    if all(isinstance(item, int) and not isinstance(item, bool) for item in items):
        return items
    raise FilterTranslationError(f"{operator} 只支持同类型的字符串列表或整数列表")


def _datetime_value(value: object) -> datetime | date:
    if isinstance(value, datetime | date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise FilterTranslationError("datetime 过滤值必须是 ISO 8601 格式") from exc
    raise FilterTranslationError("datetime 过滤值必须是日期、时间或 ISO 8601 字符串")


def _positive_condition(
    filter_: ComparisonFilter,
    index_types: dict[str, PayloadIndexType],
) -> models.FieldCondition:
    key = payload_field_path(filter_.key)
    operator = filter_.type

    if operator in {"eq", "ne"}:
        if not isinstance(filter_.value, str | int | bool):
            raise FilterTranslationError(f"{operator} 只支持字符串、整数或布尔值")
        return models.FieldCondition(key=key, match=models.MatchValue(value=filter_.value))

    if operator in {"in", "nin"}:
        return models.FieldCondition(
            key=key,
            match=models.MatchAny(any=_require_sequence(filter_.value, operator)),
        )

    declared_type = index_types.get(filter_.key)
    if declared_type is PayloadIndexType.DATETIME:
        range_value = _datetime_value(filter_.value)
        return models.FieldCondition(key=key, range=models.DatetimeRange(**{operator: range_value}))
    if declared_type not in {PayloadIndexType.INTEGER, PayloadIndexType.FLOAT}:
        raise FilterTranslationError(f"范围过滤字段 {filter_.key} 必须声明为 integer、float 或 datetime")
    if not isinstance(filter_.value, int | float) or isinstance(filter_.value, bool):
        raise FilterTranslationError(f"{operator} 的值必须是数字")
    return models.FieldCondition(key=key, range=models.Range(**{operator: float(filter_.value)}))


def translate_filter(
    filter_: ComparisonFilter | CompoundFilter,
    index_types: dict[str, PayloadIndexType] | None = None,
) -> QdrantCondition:
    """递归翻译过滤 AST；否定条件要求字段存在，避免缺失权限字段被误放行。"""

    declared_types = index_types or {}
    if isinstance(filter_, ComparisonFilter):
        condition = _positive_condition(filter_, declared_types)
        if filter_.type not in {"ne", "nin"}:
            return condition
        return models.Filter(
            must_not=[
                models.IsEmptyCondition(is_empty=models.PayloadField(key=condition.key)),
                condition,
            ]
        )

    if not filter_.filters:
        raise FilterTranslationError(f"{filter_.type} 过滤条件至少需要一个子条件")
    translated = [translate_filter(child, declared_types) for child in filter_.filters]
    if filter_.type == "and":
        return models.Filter(must=translated)
    return models.Filter(should=translated)


def scoped_filter(
    vector_store_ids: str | Sequence[str],
    filter_: ComparisonFilter | CompoundFilter | None = None,
    index_types: dict[str, PayloadIndexType] | None = None,
) -> models.Filter:
    """强制加入一个或多个逻辑 VectorStore 范围，调用方无法省略或覆盖。"""

    ids = [vector_store_ids] if isinstance(vector_store_ids, str) else list(vector_store_ids)
    if not ids or any(not vector_store_id for vector_store_id in ids):
        raise FilterTranslationError("vector_store_ids 至少需要一个非空值")

    scope_match: models.Match = models.MatchValue(value=ids[0]) if len(ids) == 1 else models.MatchAny(any=ids)

    conditions: list[QdrantCondition] = [
        models.FieldCondition(
            key="vector_store_id",
            match=scope_match,
        )
    ]
    if filter_ is not None:
        conditions.append(translate_filter(filter_, index_types))
    return models.Filter(must=conditions)
