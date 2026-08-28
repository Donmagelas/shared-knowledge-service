"""统一 Knowledge API Provider 需要实现的协议。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from fastapi import UploadFile
from starlette.responses import Response

from .errors import ApiSecurity
from .models import (
    AttributeValue,
    BatchIngestResponse,
    EmbeddingConfigPutRequest,
    EmbeddingConfigResponse,
    FileDetail,
    FileQueryRequest,
    FileQueryResponse,
    IngestResponse,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseInferenceConfigResponse,
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

    async def create_knowledge_base(
        self,
        request: KnowledgeBaseCreateRequest,
        idempotency_key: str,
    ) -> KnowledgeBaseResponse: ...

    async def get_knowledge_base(self, knowledge_base_id: str) -> KnowledgeBaseResponse: ...

    async def get_inference_config(
        self,
        knowledge_base_id: str,
    ) -> KnowledgeBaseInferenceConfigResponse: ...

    async def put_embedding_config(
        self,
        knowledge_base_id: str,
        request: EmbeddingConfigPutRequest,
    ) -> EmbeddingConfigResponse: ...

    async def put_rerank_config(
        self,
        knowledge_base_id: str,
        request: RerankConfigPutRequest,
    ) -> RerankConfigResponse: ...

    async def delete_knowledge_base(self, knowledge_base_id: str) -> None: ...

    async def ingest(
        self,
        file: UploadFile,
        knowledge_base_id: str,
        attributes: dict[str, AttributeValue],
        idempotency_key: str,
    ) -> IngestResponse: ...

    async def batch_ingest(
        self,
        files: list[UploadFile],
        knowledge_base_id: str,
        attributes: dict[str, AttributeValue],
        idempotency_key: str,
    ) -> BatchIngestResponse: ...

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

    async def download_file(self, knowledge_base_id: str, file_id: str) -> Response: ...

    async def delete_file(self, knowledge_base_id: str, file_id: str) -> None: ...

    async def search(self, request: SearchRequest) -> SearchResponse: ...
