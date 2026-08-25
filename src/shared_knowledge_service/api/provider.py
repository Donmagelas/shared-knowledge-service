"""复用 OGX Files、VectorStore 与自定义 Qdrant Provider 的 Knowledge API 实现。"""

from __future__ import annotations

from typing import Any, cast

from fastapi import UploadFile
from ogx.log import get_logger
from ogx.providers.utils.inference.prompt_adapter import interleaved_content_as_str
from ogx.providers.utils.vector_io.filters import parse_filter
from ogx_api import (
    Api,
    Files,
    InlineProviderSpec,
    OpenAICreateVectorStoreFileBatchRequestWithExtraBody,
    QueryChunksResponse,
    UploadFileRequest,
    VectorIO,
)
from ogx_api.files.models import OpenAIFileUploadPurpose
from pydantic import BaseModel, ConfigDict

from shared_knowledge_service.provider.adapter import SharedQdrantVectorIOAdapter

from .models import (
    AttributeValue,
    IngestLastError,
    IngestOperationResponse,
    IngestOperationStatus,
    IngestResponse,
    SearchHit,
    SearchRequest,
    SearchResponse,
)

log = get_logger(name=__name__, category="providers")

# 这些字段由文件处理、索引或引用定位链路生成，产品不能借 attributes 伪造。
RESERVED_INGEST_ATTRIBUTES = frozenset(
    {
        "attributes",
        "chunk_content",
        "chunk_id",
        "chunk_window",
        "content_text",
        "document_id",
        "file_id",
        "filename",
        "headings",
        "source",
        "vector_store_id",
    }
)

_SEARCH_INTERNAL_METADATA = frozenset(
    {
        "chunk_id",
        "chunk_window",
        "document_id",
        "file_id",
        "filename",
        "headings",
        "source",
    }
)


class KnowledgeApiConfig(BaseModel):
    """MVP 没有独立配置；保留空模型以符合 OGX Provider 规范。"""

    model_config = ConfigDict(extra="forbid")


class KnowledgeApiProvider:
    """把产品友好接口转换成 OGX 原生对象和共享 Qdrant 查询。"""

    def __init__(self, files_api: Files, vector_io: VectorIO) -> None:
        self.files_api = files_api
        self.vector_io = vector_io

    async def _shared_provider(self, knowledge_base_ids: list[str]) -> SharedQdrantVectorIOAdapter:
        """校验逻辑知识库存在，并解析到同一个共享 Qdrant Provider。"""

        routing_table = getattr(self.vector_io, "routing_table", None)
        if routing_table is None or not hasattr(routing_table, "get_provider_impl"):
            raise RuntimeError("VectorIO 没有可用的 OGX RoutingTable")

        providers: list[Any] = []
        for knowledge_base_id in knowledge_base_ids:
            # 先走公开读取方法，保留 OGX 的存在性和访问策略检查。
            await self.vector_io.openai_retrieve_vector_store(knowledge_base_id)
            providers.append(await routing_table.get_provider_impl(knowledge_base_id))

        first = providers[0]
        if not isinstance(first, SharedQdrantVectorIOAdapter):
            raise ValueError("knowledge_base_ids 必须由 shared-qdrant Provider 管理")
        if any(provider is not first for provider in providers[1:]):
            raise ValueError("一次检索中的 knowledge_base_ids 必须属于同一个 Provider")
        return first

    async def ingest(
        self,
        file: UploadFile,
        knowledge_base_id: str,
        attributes: dict[str, AttributeValue],
    ) -> IngestResponse:
        """持久化原文件和单文件 FileBatch，并立即返回异步任务标识。"""

        normalized_id = knowledge_base_id.strip()
        if not normalized_id:
            raise ValueError("knowledge_base_id 不能为空")
        forbidden = RESERVED_INGEST_ATTRIBUTES.intersection(attributes)
        if forbidden:
            raise ValueError(f"attributes 不能覆盖保留字段：{', '.join(sorted(forbidden))}")
        # 上传前验证知识库，避免明显的错误 ID 产生孤立原文件。
        await self._shared_provider([normalized_id])
        uploaded = await self.files_api.openai_upload_file(
            UploadFileRequest(purpose=OpenAIFileUploadPurpose.ASSISTANTS),
            file,
        )

        try:
            batch = await self.vector_io.openai_create_vector_store_file_batch(
                vector_store_id=normalized_id,
                params=OpenAICreateVectorStoreFileBatchRequestWithExtraBody(
                    file_ids=[uploaded.id],
                    attributes=attributes,
                ),
            )
        except Exception as exc:
            # Batch 是否已经持久化在异常边界上无法完全确认，因此不自动删除原文件。
            raise RuntimeError(f"异步导入任务创建失败，原文件已保存为 {uploaded.id}：{exc}") from exc

        return IngestResponse(
            operation_id=batch.id,
            file_id=uploaded.id,
            knowledge_base_id=normalized_id,
            status="processing",
        )

    async def get_ingest_operation(
        self,
        knowledge_base_id: str,
        operation_id: str,
    ) -> IngestOperationResponse:
        """把 OGX 单文件 FileBatch 状态转换成稳定的异步导入状态。"""

        normalized_knowledge_base_id = knowledge_base_id.strip()
        normalized_operation_id = operation_id.strip()
        if not normalized_knowledge_base_id:
            raise ValueError("knowledge_base_id 不能为空")
        if not normalized_operation_id:
            raise ValueError("operation_id 不能为空")

        batch = await self.vector_io.openai_retrieve_vector_store_file_batch(
            batch_id=normalized_operation_id,
            vector_store_id=normalized_knowledge_base_id,
        )
        status = self._operation_status(batch.status, batch.file_counts.model_dump())
        last_error = None
        if status == "failed":
            last_error = await self._operation_last_error(
                normalized_knowledge_base_id,
                normalized_operation_id,
            )
        return IngestOperationResponse(
            operation_id=normalized_operation_id,
            knowledge_base_id=normalized_knowledge_base_id,
            status=status,
            last_error=last_error,
        )

    @staticmethod
    def _operation_status(batch_status: str, file_counts: dict[str, int]) -> IngestOperationStatus:
        """修正 OGX Batch completed 仍可能包含失败文件的状态语义。"""

        if batch_status == "in_progress":
            return "processing"
        if batch_status == "cancelled":
            return "cancelled"
        if batch_status == "failed" or file_counts.get("failed", 0) > 0:
            return "failed"
        if batch_status == "completed" and file_counts.get("completed", 0) == file_counts.get("total", 0) == 1:
            return "completed"
        return "failed"

    async def _operation_last_error(self, knowledge_base_id: str, operation_id: str) -> IngestLastError:
        """尽量读取单文件 Batch 的具体错误，缺失时返回稳定的通用错误。"""

        try:
            files = await self.vector_io.openai_list_files_in_vector_store_file_batch(
                batch_id=operation_id,
                vector_store_id=knowledge_base_id,
                limit=1,
            )
        except Exception as exc:
            log.warning(
                "Failed to load file-level error for ingest operation",
                operation_id=operation_id,
                vector_store_id=knowledge_base_id,
                error=str(exc),
            )
            return IngestLastError(code="operation_failed", message="异步导入失败，文件级错误读取失败")
        if files.data and files.data[0].last_error is not None:
            error = files.data[0].last_error
            return IngestLastError(code=error.code, message=error.message)
        return IngestLastError(code="operation_failed", message="异步导入失败，但 OGX 未返回文件级错误")

    async def search(self, request: SearchRequest) -> SearchResponse:
        """执行一次带强制知识库范围和可选业务 Filter 的 Qdrant 查询。"""

        provider = await self._shared_provider(request.knowledge_base_ids)
        filters = parse_filter(request.filters) if request.filters is not None else None
        result = await provider.query_multiple_vector_stores(
            vector_store_ids=request.knowledge_base_ids,
            query=request.query,
            mode=request.mode,
            limit=request.limit,
            filters=filters,
        )
        return SearchResponse(hits=self._search_hits(result))

    @staticmethod
    def _search_hits(result: QueryChunksResponse) -> list[SearchHit]:
        """把内部 Chunk 转换为产品稳定结构，并分离引用定位与业务 attributes。"""

        hits: list[SearchHit] = []
        for chunk, score in zip(result.chunks, result.scores, strict=True):
            metadata = dict(chunk.metadata or {})
            file_id = str(
                metadata.get("file_id")
                or (chunk.chunk_metadata.document_id if chunk.chunk_metadata else None)
                or metadata.get("document_id")
                or ""
            )
            locator: dict[str, Any] = {}
            if chunk.chunk_metadata:
                if chunk.chunk_metadata.source:
                    locator["source"] = chunk.chunk_metadata.source
                if chunk.chunk_metadata.chunk_window is not None:
                    locator["chunk_window"] = chunk.chunk_metadata.chunk_window
            headings = metadata.get("headings")
            if isinstance(headings, list) and all(isinstance(item, str) for item in headings):
                locator["headings"] = headings

            attributes = {
                key: cast(AttributeValue, value)
                for key, value in metadata.items()
                if key not in _SEARCH_INTERNAL_METADATA and isinstance(value, str | int | float | bool)
            }
            hits.append(
                SearchHit(
                    file_id=file_id,
                    chunk_id=chunk.chunk_id,
                    content=interleaved_content_as_str(chunk.content),
                    locator=locator,
                    score=score,
                    attributes=attributes,
                )
            )
        return hits


def get_provider_spec() -> InlineProviderSpec:
    """返回统一 Knowledge API 的 OGX Provider 规格。"""

    return InlineProviderSpec(
        api=Api("knowledge"),
        provider_type="inline::shared-knowledge",
        config_class="shared_knowledge_service.api.provider.KnowledgeApiConfig",
        module="shared_knowledge_service.api",
        api_dependencies=[Api.files, Api.vector_io],
        is_external=True,
        description="Stella and Cherry Studio Enterprise shared Knowledge API.",
    )


def available_providers() -> list[InlineProviderSpec]:
    """让 OGX 为外部 API 发现内置的唯一实现。"""

    return [get_provider_spec()]


async def get_provider_impl(config: KnowledgeApiConfig, deps: dict[Api, Any]) -> KnowledgeApiProvider:
    """从 OGX 注入的 Files 与 VectorIO 构造实现。"""

    del config
    return KnowledgeApiProvider(files_api=deps[Api.files], vector_io=deps[Api.vector_io])
