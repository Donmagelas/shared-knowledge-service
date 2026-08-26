"""统一 Knowledge API Provider 需要实现的协议。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from fastapi import UploadFile

from .errors import ApiSecurity
from .models import (
    AttributeValue,
    EmbeddingConfigPutRequest,
    EmbeddingConfigResponse,
    FileDetail,
    FileQueryRequest,
    FileQueryResponse,
    IngestResponse,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseResponse,
    OperationResponse,
    RerankConfigPutRequest,
    RerankConfigResponse,
    RetryOperationResponse,
    SearchRequest,
    SearchResponse,
)


@runtime_checkable
class KnowledgeApi(Protocol):
    """Stella 与 Cherry Studio 企业版共用的完整 V1 服务契约。"""

    security: ApiSecurity

    async def put_embedding_config(
        self,
        tenant_id: str,
        request: EmbeddingConfigPutRequest,
    ) -> EmbeddingConfigResponse: ...

    async def get_embedding_config(self, tenant_id: str) -> EmbeddingConfigResponse: ...

    async def put_rerank_config(
        self,
        tenant_id: str,
        request: RerankConfigPutRequest,
    ) -> RerankConfigResponse: ...

    async def get_rerank_config(self, tenant_id: str) -> RerankConfigResponse: ...

    async def create_knowledge_base(
        self,
        request: KnowledgeBaseCreateRequest,
        idempotency_key: str,
    ) -> KnowledgeBaseResponse: ...

    async def get_knowledge_base(self, knowledge_base_id: str) -> KnowledgeBaseResponse: ...

    async def delete_knowledge_base(self, knowledge_base_id: str) -> None: ...

    async def ingest(
        self,
        file: UploadFile,
        knowledge_base_id: str,
        attributes: dict[str, AttributeValue],
        idempotency_key: str,
    ) -> IngestResponse: ...

    async def get_ingest_operation(
        self,
        operation_id: str,
    ) -> OperationResponse: ...

    async def retry_ingest_operation(
        self,
        operation_id: str,
    ) -> RetryOperationResponse: ...

    async def query_files(
        self,
        knowledge_base_id: str,
        request: FileQueryRequest,
    ) -> FileQueryResponse: ...

    async def get_file(self, knowledge_base_id: str, file_id: str) -> FileDetail: ...

    async def delete_file(self, knowledge_base_id: str, file_id: str) -> None: ...

    async def search(self, request: SearchRequest) -> SearchResponse: ...
