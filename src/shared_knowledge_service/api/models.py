"""统一 Knowledge API 的稳定请求与响应模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

type AttributeValue = str | float | bool


class IngestLastError(BaseModel):
    """同步导入失败时返回的稳定错误信息。"""

    code: str
    message: str


class IngestResponse(BaseModel):
    """一次同步上传和建索引的结果。"""

    file_id: str
    knowledge_base_id: str
    status: Literal["completed", "failed"]
    last_error: IngestLastError | None = None


class SearchRequest(BaseModel):
    """由产品完成权限计算后提交的检索请求。"""

    query: str = Field(min_length=1, max_length=32_768)
    knowledge_base_ids: list[str] = Field(min_length=1, max_length=100)
    filters: dict[str, Any] | None = None
    mode: Literal["hybrid", "dense", "bm25"] = "hybrid"
    limit: int = Field(default=10, ge=1, le=50)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        """拒绝只有空白字符的检索文本。"""

        normalized = value.strip()
        if not normalized:
            raise ValueError("query 不能为空")
        return normalized

    @field_validator("knowledge_base_ids")
    @classmethod
    def normalize_knowledge_base_ids(cls, values: list[str]) -> list[str]:
        """清理空白并稳定去重，避免重复扩大过滤表达式。"""

        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            knowledge_base_id = value.strip()
            if not knowledge_base_id:
                raise ValueError("knowledge_base_ids 不能包含空值")
            if knowledge_base_id not in seen:
                seen.add(knowledge_base_id)
                normalized.append(knowledge_base_id)
        return normalized


class SearchHit(BaseModel):
    """不暴露 OGX EmbeddedChunk 或 Qdrant Point 的稳定命中结构。"""

    file_id: str
    chunk_id: str
    content: str
    locator: dict[str, Any] = Field(default_factory=dict)
    score: float
    attributes: dict[str, AttributeValue] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    """统一检索响应。"""

    hits: list[SearchHit]
