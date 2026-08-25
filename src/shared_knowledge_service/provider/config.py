"""外置 Qdrant Provider 配置。"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Any

from ogx.core.storage.datatypes import KVStoreReference, SqlStoreReference
from pydantic import BaseModel, Field


class PayloadIndexType(StrEnum):
    """部署方可声明的 Qdrant Payload Index 类型。"""

    KEYWORD = "keyword"
    INTEGER = "integer"
    FLOAT = "float"
    BOOL = "bool"
    DATETIME = "datetime"
    TEXT = "text"


def tenant_collection_name(base_collection_name: str, tenant_id: str | None) -> str:
    """把可信租户 ID 稳定映射到物理 Collection；None 表示单租户默认路由。"""

    if tenant_id is None:
        return base_collection_name
    normalized = tenant_id.strip()
    if not normalized:
        raise ValueError("VectorStore metadata.tenant_id 不能为空")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]
    return f"{base_collection_name}__tenant_{digest}"


class SharedQdrantVectorIOConfig(BaseModel):
    """固定 OGX v1.3.0 所需连接和元数据配置。

    这里显式拥有配置契约，避免后续 Provider 被 OGX 内部 Qdrant Config 的变化绑死。
    """

    location: str | None = None
    url: str | None = None
    port: int | None = 6333
    grpc_port: int = 6334
    prefer_grpc: bool = False
    https: bool | None = None
    api_key: str | None = None
    prefix: str | None = None
    timeout: int | None = None
    host: str | None = None
    persistence: KVStoreReference
    metadata_store: SqlStoreReference | None = Field(
        default=None,
        description="用于保存 VectorStore 元数据的 OGX SQL Store",
    )
    collection_name: str = Field(
        default="shared_knowledge",
        min_length=1,
        max_length=220,
        description="单租户默认 Collection 名，同时作为多租户 Collection 名前缀",
    )
    dense_vector_name: str = Field(default="dense", min_length=1)
    sparse_vector_name: str = Field(default="bm25", min_length=1)
    payload_indexes: dict[str, PayloadIndexType] = Field(
        default_factory=dict,
        description="需要高性能过滤的业务 attributes 字段及其类型",
    )
    rerank_enabled: bool = Field(
        default=False,
        description="是否在 Hybrid RRF 候选集后调用远程神经 Reranker",
    )
    rerank_model: str = Field(
        default="qwen/qwen3-reranker-0.6b",
        min_length=1,
        description="Rerank Provider 中注册的原始模型 ID",
    )
    rerank_candidate_limit: int = Field(
        default=50,
        ge=1,
        le=200,
        description="启用 Rerank 时从 Qdrant RRF 阶段取得的最大候选数",
    )

    def collection_name_for_tenant(self, tenant_id: str | None) -> str:
        """把可信租户 ID 稳定映射到物理 Collection，避免把原始业务 ID 暴露给 Qdrant。"""

        return tenant_collection_name(self.collection_name, tenant_id)

    def qdrant_client_kwargs(self) -> dict[str, Any]:
        """只返回 AsyncQdrantClient 接受的连接参数。"""

        return self.model_dump(
            exclude_none=True,
            exclude={
                "collection_name",
                "dense_vector_name",
                "metadata_store",
                "payload_indexes",
                "persistence",
                "rerank_candidate_limit",
                "rerank_enabled",
                "rerank_model",
                "sparse_vector_name",
            },
        )
