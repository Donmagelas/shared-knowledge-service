"""统一 Knowledge API 契约与薄包装行为单元测试。"""

from __future__ import annotations

from io import BytesIO
from typing import Any

import pytest
from fastapi import UploadFile
from ogx_api import (
    ChunkMetadata,
    EmbeddedChunk,
    OpenAIFileObject,
    OpenAIFilePurpose,
    QueryChunksResponse,
    VectorStoreChunkingStrategyAuto,
    VectorStoreFileBatchObject,
    VectorStoreFileCounts,
    VectorStoreFileLastError,
    VectorStoreFileObject,
    VectorStoreFilesListInBatchResponse,
)

from shared_knowledge_service.api.models import SearchRequest
from shared_knowledge_service.api.provider import KnowledgeApiProvider
from shared_knowledge_service.api.routes import parse_attributes
from shared_knowledge_service.provider.adapter import SharedQdrantVectorIOAdapter


class FakeFiles:
    """只记录上传调用，不持久化数据。"""

    def __init__(self) -> None:
        self.upload_count = 0

    async def openai_upload_file(self, request: Any, file: UploadFile) -> OpenAIFileObject:
        del request, file
        self.upload_count += 1
        return OpenAIFileObject(
            id="file-1",
            bytes=12,
            created_at=1,
            filename="knowledge.md",
            purpose=OpenAIFilePurpose.ASSISTANTS,
            status="uploaded",
        )


class FakeSharedAdapter(SharedQdrantVectorIOAdapter):
    """跳过真实连接，只暴露跨知识库查询入口。"""

    def __init__(self, result: QueryChunksResponse) -> None:
        self.result = result
        self.search_calls: list[dict[str, Any]] = []

    async def query_multiple_vector_stores(self, **kwargs: Any) -> QueryChunksResponse:
        self.search_calls.append(kwargs)
        return self.result


class FakeRoutingTable:
    def __init__(self, provider: FakeSharedAdapter) -> None:
        self.provider = provider

    async def get_provider_impl(self, knowledge_base_id: str) -> FakeSharedAdapter:
        del knowledge_base_id
        return self.provider


class FakeVectorIO:
    """模拟 OGX VectorIORouter 的公开方法和内部 RoutingTable。"""

    def __init__(self, provider: FakeSharedAdapter) -> None:
        self.routing_table = FakeRoutingTable(provider)
        self.retrieved: list[str] = []
        self.created_batches: list[tuple[str, Any]] = []
        self.batch = VectorStoreFileBatchObject(
            id="batch-1",
            created_at=1,
            vector_store_id="vs-a",
            status="in_progress",
            file_counts=VectorStoreFileCounts(
                completed=0,
                cancelled=0,
                failed=0,
                in_progress=1,
                total=1,
            ),
        )
        self.batch_files: list[VectorStoreFileObject] = []

    async def openai_retrieve_vector_store(self, knowledge_base_id: str) -> object:
        self.retrieved.append(knowledge_base_id)
        return object()

    async def openai_create_vector_store_file_batch(
        self,
        vector_store_id: str,
        params: Any,
    ) -> VectorStoreFileBatchObject:
        self.created_batches.append((vector_store_id, params))
        return self.batch

    async def openai_retrieve_vector_store_file_batch(
        self,
        batch_id: str,
        vector_store_id: str,
    ) -> VectorStoreFileBatchObject:
        assert batch_id == self.batch.id
        assert vector_store_id == self.batch.vector_store_id
        return self.batch

    async def openai_list_files_in_vector_store_file_batch(
        self,
        batch_id: str,
        vector_store_id: str,
        **kwargs: Any,
    ) -> VectorStoreFilesListInBatchResponse:
        del kwargs
        assert batch_id == self.batch.id
        assert vector_store_id == self.batch.vector_store_id
        first_id = self.batch_files[0].id if self.batch_files else ""
        last_id = self.batch_files[-1].id if self.batch_files else ""
        return VectorStoreFilesListInBatchResponse(
            data=self.batch_files,
            first_id=first_id,
            last_id=last_id,
            has_more=False,
        )


def _query_result() -> QueryChunksResponse:
    chunk = EmbeddedChunk(
        content="退款需要订单编号。",
        chunk_id="chunk-1",
        metadata={
            "document_id": "docling-random-id",
            "file_id": "file-1",
            "filename": "knowledge.md",
            "headings": ["退款"],
            "department_id": "product-a",
        },
        chunk_metadata=ChunkMetadata(
            document_id="file-1",
            source="knowledge.md",
            chunk_window="0",
        ),
        embedding=[1.0, 0.0, 0.0],
        embedding_model="embedding/test",
        embedding_dimension=3,
    )
    return QueryChunksResponse(chunks=[chunk], scores=[0.75])


def test_search_request_deduplicates_ids_without_changing_order() -> None:
    request = SearchRequest(query=" 退款 ", knowledge_base_ids=["vs-a", "vs-b", "vs-a"])

    assert request.query == "退款"
    assert request.knowledge_base_ids == ["vs-a", "vs-b"]


def test_parse_attributes_requires_json_object() -> None:
    assert parse_attributes('{"department_id":"product-a"}') == {"department_id": "product-a"}
    with pytest.raises(ValueError, match="JSON 对象"):
        parse_attributes('["product-a"]')


@pytest.mark.asyncio
async def test_ingest_creates_single_file_batch_and_returns_without_waiting() -> None:
    files = FakeFiles()
    adapter = FakeSharedAdapter(_query_result())
    vector_io = FakeVectorIO(adapter)
    provider = KnowledgeApiProvider(files_api=files, vector_io=vector_io)  # type: ignore[arg-type]
    upload = UploadFile(file=BytesIO(b"knowledge"), filename="knowledge.md")

    response = await provider.ingest(upload, "vs-a", {"department_id": "product-a"})

    assert response.status == "processing"
    assert response.operation_id == "batch-1"
    assert response.file_id == "file-1"
    assert files.upload_count == 1
    assert vector_io.retrieved == ["vs-a"]
    assert vector_io.created_batches[0][0] == "vs-a"
    assert vector_io.created_batches[0][1].file_ids == ["file-1"]
    assert vector_io.created_batches[0][1].attributes == {"department_id": "product-a"}


@pytest.mark.asyncio
async def test_ingest_operation_maps_failed_file_count_and_error() -> None:
    adapter = FakeSharedAdapter(_query_result())
    vector_io = FakeVectorIO(adapter)
    vector_io.batch = VectorStoreFileBatchObject(
        id="batch-1",
        created_at=1,
        vector_store_id="vs-a",
        status="completed",
        file_counts=VectorStoreFileCounts(
            completed=0,
            cancelled=0,
            failed=1,
            in_progress=0,
            total=1,
        ),
    )
    vector_io.batch_files = [
        VectorStoreFileObject(
            id="file-1",
            vector_store_id="vs-a",
            chunking_strategy=VectorStoreChunkingStrategyAuto(),
            created_at=1,
            status="failed",
            last_error=VectorStoreFileLastError(code="server_error", message="embedding unavailable"),
        )
    ]
    provider = KnowledgeApiProvider(files_api=FakeFiles(), vector_io=vector_io)  # type: ignore[arg-type]

    response = await provider.get_ingest_operation("vs-a", "batch-1")

    assert response.status == "failed"
    assert response.last_error is not None
    assert response.last_error.message == "embedding unavailable"


@pytest.mark.asyncio
async def test_ingest_rejects_reserved_attributes_before_upload() -> None:
    files = FakeFiles()
    adapter = FakeSharedAdapter(_query_result())
    provider = KnowledgeApiProvider(files_api=files, vector_io=FakeVectorIO(adapter))  # type: ignore[arg-type]
    upload = UploadFile(file=BytesIO(b"knowledge"), filename="knowledge.md")

    with pytest.raises(ValueError, match="保留字段"):
        await provider.ingest(upload, "vs-a", {"vector_store_id": "vs-b"})
    assert files.upload_count == 0


@pytest.mark.asyncio
async def test_search_uses_one_provider_call_for_multiple_knowledge_bases() -> None:
    adapter = FakeSharedAdapter(_query_result())
    vector_io = FakeVectorIO(adapter)
    provider = KnowledgeApiProvider(files_api=FakeFiles(), vector_io=vector_io)  # type: ignore[arg-type]

    response = await provider.search(
        SearchRequest(
            query="退款材料",
            knowledge_base_ids=["vs-company", "vs-product-a"],
            filters={"type": "eq", "key": "department_id", "value": "product-a"},
        )
    )

    assert vector_io.retrieved == ["vs-company", "vs-product-a"]
    assert len(adapter.search_calls) == 1
    assert adapter.search_calls[0]["vector_store_ids"] == ["vs-company", "vs-product-a"]
    assert response.hits[0].file_id == "file-1"
    assert response.hits[0].locator == {
        "source": "knowledge.md",
        "chunk_window": "0",
        "headings": ["退款"],
    }
    assert response.hits[0].attributes == {"department_id": "product-a"}
