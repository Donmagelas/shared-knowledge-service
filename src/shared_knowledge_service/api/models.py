"""统一 Knowledge API 的稳定请求与响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

type AttributeScalar = str | int | float | bool
type AttributeValue = AttributeScalar | list[AttributeScalar]
type OperationStatus = Literal["processing", "completed", "failed", "cancelled"]


def _filter_shape(value: object, *, depth: int = 1) -> tuple[int, int]:
    """返回 Filter AST 的最大深度和叶子数，并拒绝明显非法的结构。"""

    if not isinstance(value, dict):
        raise ValueError("filters 必须是 JSON 对象")
    filter_type = value.get("type")
    if filter_type in {"and", "or"}:
        children = value.get("filters")
        if not isinstance(children, list) or not children:
            raise ValueError(f"{filter_type} 过滤条件至少需要一个子条件")
        child_shapes = [_filter_shape(child, depth=depth + 1) for child in children]
        return max(item[0] for item in child_shapes), sum(item[1] for item in child_shapes)
    if filter_type not in {"eq", "ne", "in", "nin", "gt", "gte", "lt", "lte"}:
        raise ValueError("filters 包含不支持的操作符")
    if not isinstance(value.get("key"), str) or not value["key"].strip():
        raise ValueError("过滤字段不能为空")
    if "value" not in value:
        raise ValueError("过滤条件缺少 value")
    return depth, 1


def validate_filter_limits(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """限制递归 Filter 的复杂度，避免产品请求放大数据库工作量。"""

    if value is None:
        return None
    depth, leaves = _filter_shape(value)
    if depth > 8:
        raise ValueError("filters 最多允许 8 层")
    if leaves > 64:
        raise ValueError("filters 最多允许 64 个叶子条件")
    return value


class ErrorBody(BaseModel):
    """可供两端稳定依赖的机器错误。"""

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    """所有同步失败共用的错误信封。"""

    error: ErrorBody
    request_id: str


class IngestLastError(BaseModel):
    """异步导入失败时返回的稳定错误信息。"""

    code: str
    message: str


class IngestResponse(BaseModel):
    """原文件与单文件 OGX Batch 已可靠创建后的提交结果。"""

    operation_id: str
    file_id: str
    knowledge_base_id: str
    status: OperationStatus
    # 只供路由选择 200/202，不能进入公开 JSON。
    replayed: bool = Field(default=False, exclude=True)


class BatchIngestItem(BaseModel):
    """批量提交中的一个独立单文件 Ingest 结果。"""

    index: int = Field(ge=0)
    filename: str
    operation_id: str
    file_id: str
    knowledge_base_id: str
    status: OperationStatus


class BatchIngestResponse(BaseModel):
    """不引入公开 Batch 实体的有序单文件结果集合。"""

    items: list[BatchIngestItem]
    # 只供路由选择 200/202，不能进入公开 JSON。
    replayed: bool = Field(default=False, exclude=True)


class OperationResponse(BaseModel):
    """单文件导入任务的稳定状态，不暴露 OGX FileBatch 结构。"""

    operation_id: str
    knowledge_base_id: str
    file_id: str
    status: OperationStatus
    created_at: datetime
    last_error: IngestLastError | None = None
    retryable: bool
    retried_from_operation_id: str | None = None
    retried_by_operation_id: str | None = None


# 保留旧名称，避免已经发布的 Python 调用方在同一次升级中断裂。
IngestOperationResponse = OperationResponse
IngestOperationStatus = OperationStatus


class RetryOperationResponse(BaseModel):
    """显式重试创建的新 Operation。"""

    operation_id: str
    knowledge_base_id: str
    file_id: str
    status: OperationStatus
    retried_from_operation_id: str
    replayed: bool = Field(default=False, exclude=True)


class SearchRequest(BaseModel):
    """由产品完成权限计算后提交的检索请求。"""

    query: str = Field(min_length=1, max_length=4096)
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

    @field_validator("filters")
    @classmethod
    def validate_filters(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return validate_filter_limits(value)


class SearchHit(BaseModel):
    """不暴露 OGX EmbeddedChunk 或 Qdrant Point 的稳定命中结构。"""

    knowledge_base_id: str
    file_id: str
    filename: str
    chunk_id: str
    content: str
    locator: dict[str, Any] = Field(default_factory=dict)
    score: float
    attributes: dict[str, AttributeValue] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    """统一检索响应。"""

    hits: list[SearchHit]


class EmbeddingConfigPutRequest(BaseModel):
    """KnowledgeBase 提交的完整 Embedding 连接配置。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    base_url: str = Field(min_length=1, max_length=2048)
    api_key: str = Field(min_length=1, max_length=8192)
    model_id: str = Field(min_length=1, max_length=512)
    dimension: int | None = Field(default=None, ge=1, le=65_536)


class EmbeddingConfigResponse(BaseModel):
    """不包含明文凭证的 KnowledgeBase Embedding 配置。"""

    base_url: str
    model_id: str
    dimension: int
    credential_configured: bool
    locked: bool
    updated_at: datetime


class RerankConfigPutRequest(BaseModel):
    """KnowledgeBase Rerank 开关与独立连接配置。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    enabled: bool
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    api_key: str | None = Field(default=None, min_length=1, max_length=8192)
    model_id: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def validate_complete_connection(self) -> RerankConfigPutRequest:
        """启用时必须完整提交；关闭时不隐式保留旧连接。"""

        values = (self.base_url, self.api_key, self.model_id)
        if self.enabled and any(value is None for value in values):
            raise ValueError("启用 Rerank 需要完整的 base_url、api_key 和 model_id")
        if not self.enabled and any(value is not None for value in values):
            raise ValueError("关闭 Rerank 时不能同时提交连接配置")
        return self


class RerankConfigCreateRequest(BaseModel):
    """创建 KnowledgeBase 时提交的可选完整 Rerank 连接。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    base_url: str = Field(min_length=1, max_length=2048)
    api_key: str = Field(min_length=1, max_length=8192)
    model_id: str = Field(min_length=1, max_length=512)


class RerankConfigResponse(BaseModel):
    """不包含明文凭证的 KnowledgeBase Rerank 配置。"""

    enabled: bool
    base_url: str | None
    model_id: str | None
    credential_configured: bool
    updated_at: datetime


class KnowledgeBaseCreateRequest(BaseModel):
    """创建技术 KnowledgeBase 时提交存储路由和完整模型配置。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    tenant_id: str = Field(min_length=1, max_length=256)
    embedding: EmbeddingConfigPutRequest
    rerank: RerankConfigCreateRequest | None = None

    @field_validator("tenant_id")
    @classmethod
    def normalize_tenant_id(cls, value: str) -> str:
        return value.strip()


class KnowledgeBaseEmbedding(BaseModel):
    """KnowledgeBase 继承的非敏感向量空间信息。"""

    model_id: str
    dimension: int
    locked: bool


class FileCounts(BaseModel):
    """统一后的知识库文件状态计数。"""

    total: int
    processing: int
    completed: int
    failed: int


class KnowledgeBaseResponse(BaseModel):
    """技术 KnowledgeBase 对账响应。"""

    knowledge_base_id: str
    tenant_id: str
    embedding: KnowledgeBaseEmbedding
    rerank: RerankConfigResponse | None
    file_counts: FileCounts
    created_at: datetime
    replayed: bool = Field(default=False, exclude=True)


class KnowledgeBaseInferenceConfigResponse(BaseModel):
    """KnowledgeBase 当前完整但不含凭证的模型配置。"""

    knowledge_base_id: str
    embedding: EmbeddingConfigResponse
    rerank: RerankConfigResponse | None


class FileQueryRequest(BaseModel):
    """文件列表使用 Filter 与 Cursor，而不是 Offset。"""

    filters: dict[str, Any] | None = None
    statuses: list[OperationStatus] | None = None
    cursor: str | None = Field(default=None, max_length=4096)
    limit: int = Field(default=20, ge=1, le=100)

    @field_validator("filters")
    @classmethod
    def validate_filters(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return validate_filter_limits(value)

    @field_validator("statuses")
    @classmethod
    def normalize_statuses(cls, value: list[OperationStatus] | None) -> list[OperationStatus] | None:
        return list(dict.fromkeys(value)) if value else None


class FileItem(BaseModel):
    """一个 KnowledgeBase 内的文件技术状态。"""

    file_id: str
    filename: str
    size_bytes: int
    status: OperationStatus
    latest_operation_id: str
    attributes: dict[str, AttributeValue]
    last_error: IngestLastError | None = None
    created_at: datetime


class FileDetail(FileItem):
    """文件详情额外回显所属 KnowledgeBase。"""

    knowledge_base_id: str


class FileQueryResponse(BaseModel):
    """Cursor 文件分页结果。"""

    items: list[FileItem]
    next_cursor: str | None
    has_more: bool


class _AttributesValidatedModel(BaseModel):
    """供路由显式验证 multipart attributes 的内部模型。"""

    attributes: dict[str, AttributeValue] = Field(default_factory=dict, max_length=16)

    @model_validator(mode="after")
    def validate_attribute_contract(self) -> _AttributesValidatedModel:
        for key, value in self.attributes.items():
            if not key.strip() or len(key) > 64:
                raise ValueError("attributes 字段名必须为 1～64 个字符")
            values = value if isinstance(value, list) else [value]
            if not values:
                raise ValueError(f"attributes.{key} 不能是空数组")
            if len(values) > 64:
                raise ValueError(f"attributes.{key} 数组最多允许 64 项")
            if any(isinstance(item, str) and len(item) > 512 for item in values):
                raise ValueError(f"attributes.{key} 的字符串值不能超过 512 个字符")
        return self


def validate_attributes(value: dict[str, Any]) -> dict[str, AttributeValue]:
    """把 multipart JSON 转换成受约束的公开 attributes。"""

    return _AttributesValidatedModel(attributes=value).attributes
