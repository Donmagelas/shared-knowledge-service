"""统一 Knowledge API 契约与薄包装行为单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, UploadFile
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from ogx_api import (
    ChunkMetadata,
    EmbeddedChunk,
    ListOpenAIFileResponse,
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
from ogx_api.router_utils import PUBLIC_ROUTE_KEY

from shared_knowledge_service.api.errors import ApiSecurity, KnowledgeError
from shared_knowledge_service.api.models import FileQueryRequest, IngestLastError, SearchRequest
from shared_knowledge_service.api.provider import KnowledgeApiProvider
from shared_knowledge_service.api.routes import create_router, parse_attributes
from shared_knowledge_service.api.state import (
    CredentialCipher,
    EmbeddingProfileRecord,
    FileRecord,
    IngestIdempotencyRecord,
    KnowledgeState,
    OperationRecord,
)
from shared_knowledge_service.provider.adapter import SharedQdrantVectorIOAdapter


class FakeFiles:
    """只记录上传调用，不持久化数据。"""

    def __init__(self) -> None:
        self.upload_count = 0
        self.files: dict[str, OpenAIFileObject] = {}
        self.deleted: list[str] = []

    async def openai_upload_file(self, request: Any, file: UploadFile) -> OpenAIFileObject:
        del request, file
        self.upload_count += 1
        result = OpenAIFileObject(
            id="file-1",
            bytes=12,
            created_at=1,
            filename="knowledge.md",
            purpose=OpenAIFilePurpose.ASSISTANTS,
            status="uploaded",
        )
        self.files[result.id] = result
        return result

    async def openai_retrieve_file(self, request: Any) -> OpenAIFileObject:
        return self.files[request.file_id]

    async def openai_list_files(self, request: Any) -> ListOpenAIFileResponse:
        del request
        data = list(self.files.values())
        return ListOpenAIFileResponse(
            data=data,
            has_more=False,
            first_id=data[0].id if data else "",
            last_id=data[-1].id if data else "",
        )

    async def openai_delete_file(self, request: Any) -> object:
        self.deleted.append(request.file_id)
        self.files.pop(request.file_id, None)
        return SimpleNamespace(id=request.file_id, deleted=True)


class FakeSharedAdapter(SharedQdrantVectorIOAdapter):
    """跳过真实连接，只暴露跨知识库查询入口。"""

    def __init__(self, result: QueryChunksResponse) -> None:
        self.result = result
        self.search_calls: list[dict[str, Any]] = []
        self.ensured: list[str] = []
        self.openai_file_batches: dict[str, dict[str, Any]] = {}

    async def query_multiple_vector_stores(self, **kwargs: Any) -> QueryChunksResponse:
        self.search_calls.append(kwargs)
        return self.result

    async def ensure_vector_store_collection(self, vector_store_id: str) -> str:
        self.ensured.append(vector_store_id)
        return "tenant-collection"


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
        self.batch_available = True
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
        self.deleted_vector_store_files: list[tuple[str, str]] = []

    async def openai_retrieve_vector_store(self, knowledge_base_id: str) -> object:
        self.retrieved.append(knowledge_base_id)
        return SimpleNamespace(
            id=knowledge_base_id,
            metadata={"tenant_id": "tenant-a"},
            created_at=1,
        )

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
        if not self.batch_available:
            raise ValueError("batch expired")
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

    async def openai_delete_vector_store_file(self, vector_store_id: str, file_id: str) -> object:
        self.deleted_vector_store_files.append((vector_store_id, file_id))
        return SimpleNamespace(id=file_id, deleted=True)


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
            "vector_store_id": "vs-company",
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


class MemoryKV:
    """单元测试使用的最小异步 KVStore。"""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)

    async def values_in_range(self, start_key: str, end_key: str) -> list[str]:
        return [value for key, value in sorted(self.data.items()) if start_key <= key < end_key]

    async def keys_in_range(self, start_key: str, end_key: str) -> list[str]:
        return [key for key in sorted(self.data) if start_key <= key < end_key]


async def _provider(files: FakeFiles, vector_io: FakeVectorIO) -> KnowledgeApiProvider:
    state = KnowledgeState(MemoryKV(), CredentialCipher("test-master-key-at-least-sixteen"))
    profile_id = state.embedding_profile_id("tenant-a")
    await state.save_embedding(
        EmbeddingProfileRecord(
            tenant_id="tenant-a",
            profile_id=profile_id,
            base_url="https://embedding.example/v1",
            model_id="embedding/test",
            dimension=3,
            credential=state.encrypt_api_key("test-key", profile_id=profile_id),
        )
    )
    return KnowledgeApiProvider(files_api=files, vector_io=vector_io, state=state)  # type: ignore[arg-type]


def test_search_request_deduplicates_ids_without_changing_order() -> None:
    request = SearchRequest(query=" 退款 ", knowledge_base_ids=["vs-a", "vs-b", "vs-a"])

    assert request.query == "退款"
    assert request.knowledge_base_ids == ["vs-a", "vs-b"]


def test_parse_attributes_requires_json_object() -> None:
    assert parse_attributes('{"department_id":"product-a"}') == {"department_id": "product-a"}
    with pytest.raises(KnowledgeError, match="JSON 对象"):
        parse_attributes('["product-a"]')


def test_internal_auth_only_accepts_admin_and_marks_knowledge_routes_public_to_ogx() -> None:
    """OGX 原生接口只能复用 Admin Token，产品路由仍由自身稳定鉴权处理。"""

    impl = SimpleNamespace(security=ApiSecurity("runtime-token-1234", "admin-token-123456"))
    router = create_router(impl)  # type: ignore[arg-type]
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    runtime_response = client.post(
        "/knowledge/v1/internal/auth/validate",
        json={"api_key": "runtime-token-1234", "request": {"path": "/v1/files"}},
    )
    admin_response = client.post(
        "/knowledge/v1/internal/auth/validate",
        json={"api_key": "admin-token-123456", "request": {"path": "/v1/files"}},
    )

    assert runtime_response.status_code == 401
    assert runtime_response.json()["error"]["code"] == "unauthorized"
    assert runtime_response.headers["X-Request-ID"] == runtime_response.json()["request_id"]
    assert admin_response.status_code == 200
    assert admin_response.json()["principal"] == "knowledge-admin"
    assert admin_response.headers["X-Request-ID"].startswith("req_")
    assert all(
        (route.openapi_extra or {}).get(PUBLIC_ROUTE_KEY) is True
        for route in router.routes
        if isinstance(route, APIRoute)
    )


@pytest.mark.asyncio
async def test_ingest_creates_single_file_batch_and_returns_without_waiting() -> None:
    files = FakeFiles()
    adapter = FakeSharedAdapter(_query_result())
    vector_io = FakeVectorIO(adapter)
    provider = await _provider(files, vector_io)
    upload = UploadFile(file=BytesIO(b"knowledge"), filename="knowledge.md")

    response = await provider.ingest(upload, "vs-a", {"department_id": "product-a"}, "ingest-1")

    assert response.status == "processing"
    assert response.operation_id == "batch-1"
    assert response.file_id == "file-1"
    assert files.upload_count == 1
    assert vector_io.retrieved == ["vs-a"]
    assert vector_io.created_batches[0][0] == "vs-a"
    assert vector_io.created_batches[0][1].file_ids == ["file-1"]
    assert vector_io.created_batches[0][1].attributes == {"department_id": "product-a"}


@pytest.mark.asyncio
async def test_ingest_replay_recovers_persisted_batch_without_creating_a_second_task() -> None:
    files = FakeFiles()
    adapter = FakeSharedAdapter(_query_result())
    vector_io = FakeVectorIO(adapter)
    provider = await _provider(files, vector_io)
    first = await provider.ingest(
        UploadFile(file=BytesIO(b"knowledge"), filename="knowledge.md"),
        "vs-a",
        {"department_id": "product-a"},
        "ingest-1",
    )
    record = await provider._state().get_ingest_idempotency("vs-a", "ingest-1")
    assert record is not None
    record.state = "file_uploaded"
    record.operation_id = None
    await provider._state().save_ingest_idempotency("ingest-1", record)
    adapter.openai_file_batches[first.operation_id] = {
        **vector_io.batch.model_dump(),
        "file_ids": [first.file_id],
        "attributes": {"department_id": "product-a"},
    }

    recovered = await provider.ingest(
        UploadFile(file=BytesIO(b"knowledge"), filename="knowledge.md"),
        "vs-a",
        {"department_id": "product-a"},
        "ingest-1",
    )

    assert recovered.operation_id == first.operation_id
    assert recovered.file_id == first.file_id
    assert len(vector_io.created_batches) == 1


def test_existing_single_file_batch_can_be_recovered_after_control_state_gap() -> None:
    adapter = FakeSharedAdapter(_query_result())
    adapter.openai_file_batches["batch-1"] = {
        "id": "batch-1",
        "created_at": 1,
        "vector_store_id": "vs-a",
        "status": "in_progress",
        "file_counts": {"completed": 0, "cancelled": 0, "failed": 0, "in_progress": 1, "total": 1},
        "file_ids": ["file-1"],
        "attributes": {"department_id": "product-a"},
    }

    recovered = adapter.find_single_file_batch("vs-a", "file-1", {"department_id": "product-a"})

    assert recovered is not None
    assert recovered.id == "batch-1"
    assert adapter.find_single_file_batch("vs-b", "file-1", {"department_id": "product-a"}) is None


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
    files = FakeFiles()
    provider = await _provider(files, vector_io)
    upload = UploadFile(file=BytesIO(b"knowledge"), filename="knowledge.md")
    await provider.ingest(upload, "vs-a", {}, "ingest-1")

    response = await provider.get_ingest_operation("vs-a", "batch-1")

    assert response.status == "failed"
    assert response.last_error is not None
    assert response.last_error.message == "embedding unavailable"

    # OGX FileBatch 到期或暂时不可读后，统一接口仍返回已持久化的终态快照。
    vector_io.batch_available = False
    persisted = await provider.get_ingest_operation("vs-a", "batch-1")
    assert persisted.status == "failed"
    assert persisted.last_error is not None
    assert persisted.last_error.message == "embedding unavailable"


@pytest.mark.asyncio
async def test_ingest_rejects_reserved_attributes_before_upload() -> None:
    files = FakeFiles()
    adapter = FakeSharedAdapter(_query_result())
    provider = await _provider(files, FakeVectorIO(adapter))
    upload = UploadFile(file=BytesIO(b"knowledge"), filename="knowledge.md")

    with pytest.raises(KnowledgeError, match="保留字段"):
        await provider.ingest(upload, "vs-a", {"vector_store_id": "vs-b"}, "ingest-1")
    assert files.upload_count == 0


@pytest.mark.asyncio
async def test_search_uses_one_provider_call_for_multiple_knowledge_bases() -> None:
    adapter = FakeSharedAdapter(_query_result())
    vector_io = FakeVectorIO(adapter)
    provider = await _provider(FakeFiles(), vector_io)

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
    assert response.hits[0].knowledge_base_id == "vs-company"
    assert response.hits[0].filename == "knowledge.md"
    assert response.hits[0].file_id == "file-1"
    assert response.hits[0].locator == {
        "source": "knowledge.md",
        "chunk_window": "0",
        "headings": ["退款"],
    }
    assert response.hits[0].attributes == {"department_id": "product-a"}


@pytest.mark.asyncio
async def test_deleting_knowledge_base_rejects_new_search() -> None:
    adapter = FakeSharedAdapter(_query_result())
    provider = await _provider(FakeFiles(), FakeVectorIO(adapter))
    await provider._state().mark_knowledge_base_deleting("vs-company")

    with pytest.raises(KnowledgeError) as captured:
        await provider.search(SearchRequest(query="退款", knowledge_base_ids=["vs-company"]))

    assert captured.value.status_code == 409
    assert captured.value.code == "knowledge_base_deleting"


@pytest.mark.asyncio
async def test_file_query_rejects_reserved_search_fields() -> None:
    adapter = FakeSharedAdapter(_query_result())
    provider = await _provider(FakeFiles(), FakeVectorIO(adapter))

    with pytest.raises(KnowledgeError) as captured:
        await provider.query_files(
            "vs-a",
            FileQueryRequest(filters={"type": "eq", "key": "vector_store_id", "value": "vs-b"}),
        )

    assert captured.value.status_code == 422
    assert captured.value.code == "invalid_filter"


@pytest.mark.asyncio
async def test_cleanup_removes_stale_uncommitted_and_orphan_files() -> None:
    """清理器只回收超过保留期且没有在途 Batch 或控制面引用的原文件。"""

    files = FakeFiles()
    adapter = FakeSharedAdapter(_query_result())
    vector_io = FakeVectorIO(adapter)
    provider = await _provider(files, vector_io)
    now = datetime(2026, 8, 25, tzinfo=UTC)
    stale = int((now - timedelta(days=2)).timestamp())
    files.files["file-uncommitted"] = OpenAIFileObject(
        id="file-uncommitted",
        bytes=12,
        created_at=stale,
        filename="uncommitted.md",
        purpose=OpenAIFilePurpose.ASSISTANTS,
        status="uploaded",
    )
    files.files["file-orphan"] = OpenAIFileObject(
        id="file-orphan",
        bytes=12,
        created_at=stale,
        filename="orphan.md",
        purpose=OpenAIFilePurpose.ASSISTANTS,
        status="uploaded",
    )
    await provider._state().save_ingest_idempotency(
        "stale-request",
        IngestIdempotencyRecord(
            knowledge_base_id="vs-a",
            key_hash="ignored-by-state-key",
            fingerprint="fingerprint",
            state="file_uploaded",
            file_id="file-uncommitted",
            created_at=now - timedelta(days=2),
        ),
    )

    result = await provider.cleanup_invalid_files(now=now)

    assert result == {"uncommitted": 1, "failed": 0, "orphan": 1}
    assert set(files.deleted) == {"file-uncommitted", "file-orphan"}
    assert vector_io.deleted_vector_store_files == [("vs-a", "file-uncommitted")]
    assert await provider._state().get_ingest_idempotency("vs-a", "stale-request") is None


@pytest.mark.asyncio
async def test_cleanup_keeps_uncommitted_file_while_batch_is_processing() -> None:
    """超过保留期的记录若仍有 OGX Batch 运行，不能被清理器误删。"""

    files = FakeFiles()
    adapter = FakeSharedAdapter(_query_result())
    vector_io = FakeVectorIO(adapter)
    provider = await _provider(files, vector_io)
    now = datetime(2026, 8, 25, tzinfo=UTC)
    files.files["file-processing"] = OpenAIFileObject(
        id="file-processing",
        bytes=12,
        created_at=int((now - timedelta(days=2)).timestamp()),
        filename="processing.md",
        purpose=OpenAIFilePurpose.ASSISTANTS,
        status="uploaded",
    )
    await provider._state().save_ingest_idempotency(
        "processing-request",
        IngestIdempotencyRecord(
            knowledge_base_id="vs-a",
            key_hash="ignored-by-state-key",
            fingerprint="fingerprint",
            state="file_uploaded",
            file_id="file-processing",
            created_at=now - timedelta(days=2),
        ),
    )
    adapter.openai_file_batches["batch-processing"] = {
        "id": "batch-processing",
        "created_at": int((now - timedelta(days=2)).timestamp()),
        "vector_store_id": "vs-a",
        "status": "in_progress",
        "file_counts": {"completed": 0, "cancelled": 0, "failed": 0, "in_progress": 1, "total": 1},
        "file_ids": ["file-processing"],
        "attributes": {},
    }

    result = await provider.cleanup_invalid_files(now=now)

    assert result == {"uncommitted": 0, "failed": 0, "orphan": 0}
    assert files.deleted == []
    assert await provider._state().get_ingest_idempotency("vs-a", "processing-request") is not None


@pytest.mark.asyncio
async def test_cleanup_removes_expired_failed_source_but_keeps_operation_history() -> None:
    """最终失败超过保留期后删除文件，Operation 历史仍可查询且不可再重试。"""

    files = FakeFiles()
    adapter = FakeSharedAdapter(_query_result())
    vector_io = FakeVectorIO(adapter)
    vector_io.batch_available = False
    provider = await _provider(files, vector_io)
    now = datetime(2026, 8, 25, tzinfo=UTC)
    terminal_at = now - timedelta(days=8)
    files.files["file-failed"] = OpenAIFileObject(
        id="file-failed",
        bytes=12,
        created_at=int((now - timedelta(days=9)).timestamp()),
        filename="failed.md",
        purpose=OpenAIFilePurpose.ASSISTANTS,
        status="uploaded",
    )
    await provider._state().save_operation(
        OperationRecord(
            operation_id="batch-failed",
            knowledge_base_id="vs-a",
            file_id="file-failed",
            created_at=terminal_at,
            status_snapshot="failed",
            last_error_snapshot=IngestLastError(code="embedding_failed", message="Embedding 服务不可用"),
            terminal_at=terminal_at,
        )
    )
    await provider._state().save_file(
        FileRecord(
            knowledge_base_id="vs-a",
            file_id="file-failed",
            filename="failed.md",
            size_bytes=12,
            latest_operation_id="batch-failed",
            created_at=terminal_at,
        )
    )

    result = await provider.cleanup_invalid_files(now=now)

    assert result == {"uncommitted": 0, "failed": 1, "orphan": 0}
    assert files.deleted == ["file-failed"]
    assert vector_io.deleted_vector_store_files == [("vs-a", "file-failed")]
    assert await provider._state().get_file("vs-a", "file-failed") is None
    assert await provider._state().get_operation("vs-a", "batch-failed") is not None
    operation = await provider.get_ingest_operation("vs-a", "batch-failed")
    assert operation.status == "failed"
    assert operation.retryable is False
    with pytest.raises(KnowledgeError) as captured:
        await provider.retry_ingest_operation("vs-a", "batch-failed")
    assert captured.value.status_code == 409
    assert captured.value.code == "retry_source_missing"


@pytest.mark.asyncio
async def test_cleanup_preserves_shared_source_and_active_retry_child() -> None:
    """另一知识库引用原文或最新重试仍在处理时，清理器不能误删共享原文件。"""

    files = FakeFiles()
    adapter = FakeSharedAdapter(_query_result())
    vector_io = FakeVectorIO(adapter)
    vector_io.batch_available = False
    provider = await _provider(files, vector_io)
    now = datetime(2026, 8, 25, tzinfo=UTC)
    terminal_at = now - timedelta(days=8)
    files.files["file-shared"] = OpenAIFileObject(
        id="file-shared",
        bytes=12,
        created_at=int((now - timedelta(days=9)).timestamp()),
        filename="shared.md",
        purpose=OpenAIFilePurpose.ASSISTANTS,
        status="uploaded",
    )
    await provider._state().save_operation(
        OperationRecord(
            operation_id="batch-failed-a",
            knowledge_base_id="vs-a",
            file_id="file-shared",
            created_at=terminal_at,
            status_snapshot="failed",
            terminal_at=terminal_at,
        )
    )
    await provider._state().save_file(
        FileRecord(
            knowledge_base_id="vs-a",
            file_id="file-shared",
            filename="shared.md",
            size_bytes=12,
            latest_operation_id="batch-failed-a",
            created_at=terminal_at,
        )
    )
    await provider._state().save_operation(
        OperationRecord(
            operation_id="batch-completed-b",
            knowledge_base_id="vs-b",
            file_id="file-shared",
            status_snapshot="completed",
            terminal_at=terminal_at,
        )
    )
    await provider._state().save_file(
        FileRecord(
            knowledge_base_id="vs-b",
            file_id="file-shared",
            filename="shared.md",
            size_bytes=12,
            latest_operation_id="batch-completed-b",
        )
    )
    await provider._state().save_operation(
        OperationRecord(
            operation_id="batch-old-failure",
            knowledge_base_id="vs-retry",
            file_id="file-retry",
            status_snapshot="failed",
            terminal_at=terminal_at,
            retried_by_operation_id="batch-active-retry",
        )
    )
    await provider._state().save_operation(
        OperationRecord(
            operation_id="batch-active-retry",
            knowledge_base_id="vs-retry",
            file_id="file-retry",
            retried_from_operation_id="batch-old-failure",
        )
    )
    await provider._state().save_file(
        FileRecord(
            knowledge_base_id="vs-retry",
            file_id="file-retry",
            filename="retry.md",
            size_bytes=12,
            latest_operation_id="batch-active-retry",
        )
    )

    result = await provider.cleanup_invalid_files(now=now)

    assert result == {"uncommitted": 0, "failed": 1, "orphan": 0}
    assert files.deleted == []
    assert await provider._state().get_file("vs-a", "file-shared") is None
    assert await provider._state().get_file("vs-b", "file-shared") is not None
    assert await provider._state().get_file("vs-retry", "file-retry") is not None
