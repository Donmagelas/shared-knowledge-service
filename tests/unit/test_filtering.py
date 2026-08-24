"""Qdrant Payload Filter 翻译的单元测试。"""

from __future__ import annotations

import pytest
from ogx_api import ComparisonFilter, CompoundFilter
from qdrant_client import models

from shared_knowledge_service.provider.config import PayloadIndexType
from shared_knowledge_service.provider.filtering import (
    FilterTranslationError,
    payload_field_path,
    scoped_filter,
    translate_filter,
)


def test_business_field_is_mapped_under_attributes() -> None:
    condition = translate_filter(ComparisonFilter(type="eq", key="department_id", value="dept-a"))

    assert isinstance(condition, models.FieldCondition)
    assert condition.key == "attributes.department_id"
    assert condition.match == models.MatchValue(value="dept-a")


def test_scope_is_always_added_by_service() -> None:
    result = scoped_filter(
        "vector-store-a",
        ComparisonFilter(type="eq", key="file_id", value="file-a"),
    )

    assert result.must is not None
    scope, file_filter = result.must
    assert isinstance(scope, models.FieldCondition)
    assert scope.key == "vector_store_id"
    assert scope.match == models.MatchValue(value="vector-store-a")
    assert isinstance(file_filter, models.FieldCondition)
    assert file_filter.key == "file_id"


def test_multiple_scopes_use_one_match_any_condition() -> None:
    result = scoped_filter(["vector-store-a", "vector-store-b"])

    assert result.must is not None and len(result.must) == 1
    scope = result.must[0]
    assert isinstance(scope, models.FieldCondition)
    assert scope.match == models.MatchAny(any=["vector-store-a", "vector-store-b"])


def test_caller_cannot_filter_vector_store_id_or_raw_payload_path() -> None:
    with pytest.raises(FilterTranslationError, match="vector_store_id"):
        payload_field_path("vector_store_id")

    with pytest.raises(FilterTranslationError, match="保留 Payload"):
        payload_field_path("attributes.department_id")


def test_nested_boolean_filter_preserves_and_or_structure() -> None:
    result = translate_filter(
        CompoundFilter(
            type="and",
            filters=[
                ComparisonFilter(type="eq", key="company_id", value="company-a"),
                CompoundFilter(
                    type="or",
                    filters=[
                        ComparisonFilter(type="in", key="department_id", value=["dept-a", "dept-b"]),
                        ComparisonFilter(type="eq", key="is_public", value=True),
                    ],
                ),
            ],
        )
    )

    assert isinstance(result, models.Filter)
    assert result.must is not None and len(result.must) == 2
    nested_or = result.must[1]
    assert isinstance(nested_or, models.Filter)
    assert nested_or.should is not None and len(nested_or.should) == 2


@pytest.mark.parametrize("operator", ["ne", "nin"])
def test_negative_filter_does_not_match_missing_field(operator: str) -> None:
    value: object = "dept-a" if operator == "ne" else ["dept-a"]
    result = translate_filter(ComparisonFilter(type=operator, key="department_id", value=value))

    assert isinstance(result, models.Filter)
    assert result.must_not is not None and len(result.must_not) == 2
    assert isinstance(result.must_not[0], models.IsEmptyCondition)
    assert isinstance(result.must_not[1], models.FieldCondition)


def test_range_filter_requires_declared_numeric_or_datetime_type() -> None:
    with pytest.raises(FilterTranslationError, match="必须声明"):
        translate_filter(ComparisonFilter(type="gte", key="priority", value=10))

    condition = translate_filter(
        ComparisonFilter(type="gte", key="priority", value=10),
        {"priority": PayloadIndexType.INTEGER},
    )
    assert isinstance(condition, models.FieldCondition)
    assert condition.key == "attributes.priority"
    assert condition.range == models.Range(gte=10.0)


def test_empty_compound_filter_is_rejected() -> None:
    with pytest.raises(FilterTranslationError, match="至少需要一个子条件"):
        translate_filter(CompoundFilter(type="and", filters=[]))
