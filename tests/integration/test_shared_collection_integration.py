"""共享 Collection 与逻辑 VectorStore 隔离的真实 Qdrant 集成验证。"""

from __future__ import annotations

import asyncio
import os
import uuid

import numpy as np
import pytest
from ogx.core.storage.datatypes import KVStoreReference
from ogx_api import ChunkMetadata, ComparisonFilter, CompoundFilter, EmbeddedChunk, VectorStore
from qdrant_client import AsyncQdrantClient

from shared_knowledge_service.provider.config import PayloadIndexType, SharedQdrantVectorIOConfig
from shared_knowledge_service.provider.index import SharedQdrantIndex

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _vector_store(identifier: str) -> VectorStore:
    return VectorStore(
        identifier=identifier,
        provider_id="shared-qdrant",
        embedding_model="test-model",
        embedding_dimension=3,
    )


def _chunk(file_id: str, department_id: str, embedding: list[float]) -> EmbeddedChunk:
    return EmbeddedChunk(
        content=f"{department_id} 的内容",
        chunk_id="same-chunk-id",
        metadata={"file_id": file_id, "department_id": department_id},
        chunk_metadata=ChunkMetadata(document_id=file_id),
        embedding=embedding,
        embedding_model="test-model",
        embedding_dimension=3,
    )


def _scope_chunk(chunk_id: str, scope: str, **owners: str) -> EmbeddedChunk:
    metadata = {"file_id": f"file-{chunk_id}", "scope": scope, **owners}
    return EmbeddedChunk(
        content=f"{scope} 的退款规则",
        chunk_id=chunk_id,
        metadata=metadata,
        chunk_metadata=ChunkMetadata(document_id=f"file-{chunk_id}"),
        embedding=[1.0, 0.0, 0.0],
        embedding_model="test-model",
        embedding_dimension=3,
    )


async def test_two_logical_vector_stores_share_collection_without_leaking() -> None:
    qdrant_url = os.environ.get("QDRANT_INTEGRATION_URL")
    if not qdrant_url:
        pytest.skip("未设置 QDRANT_INTEGRATION_URL")

    collection_name = f"integration_{uuid.uuid4().hex}"
    config = SharedQdrantVectorIOConfig(
        url=qdrant_url,
        persistence=KVStoreReference(backend="test", namespace="test"),
        collection_name=collection_name,
        payload_indexes={
            "agent_id": PayloadIndexType.KEYWORD,
            "department_id": PayloadIndexType.KEYWORD,
            "scope": PayloadIndexType.KEYWORD,
            "user_id": PayloadIndexType.KEYWORD,
        },
    )
    # 本地测试地址不应经过开发机代理，否则代理依赖会掩盖真实 Qdrant 行为。
    client = AsyncQdrantClient(url=qdrant_url, trust_env=False, check_compatibility=False)
    collection_lock = asyncio.Lock()
    index_a = SharedQdrantIndex(client, _vector_store("vs-a"), config, collection_lock, collection_name)
    index_b = SharedQdrantIndex(client, _vector_store("vs-b"), config, collection_lock, collection_name)

    try:
        await index_a.initialize()
        await index_b.initialize()
        await index_a.add_chunks([_chunk("file-a", "dept-a", [1.0, 0.0, 0.0])])
        await index_b.add_chunks([_chunk("file-b", "dept-b", [1.0, 0.0, 0.0])])

        count = await client.count(collection_name=collection_name, exact=True)
        assert count.count == 2

        result_a = await index_a.query_vector(np.asarray([1.0, 0.0, 0.0]), 10, 0.0)
        assert [chunk.metadata["department_id"] for chunk in result_a.chunks] == ["dept-a"]

        result_b = await index_b.query_vector(
            np.asarray([1.0, 0.0, 0.0]),
            10,
            0.0,
            ComparisonFilter(type="eq", key="department_id", value="dept-b"),
        )
        assert [chunk.metadata["department_id"] for chunk in result_b.chunks] == ["dept-b"]

        keyword_b = await index_b.query_keyword("dept-b 内容", 10, 0.0)
        assert [chunk.metadata["department_id"] for chunk in keyword_b.chunks] == ["dept-b"]

        hybrid_b = await index_b.query_hybrid(
            np.asarray([1.0, 0.0, 0.0]),
            "dept-b 内容",
            10,
            0.0,
            "rrf",
        )
        assert [chunk.metadata["department_id"] for chunk in hybrid_b.chunks] == ["dept-b"]

        multi_store = await index_a.query_vector_scoped(
            ["vs-a", "vs-b"],
            np.asarray([1.0, 0.0, 0.0]),
            10,
            0.0,
        )
        assert {chunk.metadata["department_id"] for chunk in multi_store.chunks} == {"dept-a", "dept-b"}

        await index_a.add_chunks(
            [
                _scope_chunk("system", "system"),
                _scope_chunk("system-agent-a", "system_agent", agent_id="agent-a"),
                _scope_chunk("user-a", "user", user_id="user-a"),
                _scope_chunk("user-agent-a", "user_agent", user_id="user-a", agent_id="agent-a"),
                _scope_chunk("other-user", "user", user_id="user-b"),
            ]
        )
        stella_filter = CompoundFilter(
            type="or",
            filters=[
                ComparisonFilter(type="eq", key="scope", value="system"),
                CompoundFilter(
                    type="and",
                    filters=[
                        ComparisonFilter(type="eq", key="scope", value="system_agent"),
                        ComparisonFilter(type="eq", key="agent_id", value="agent-a"),
                    ],
                ),
                CompoundFilter(
                    type="and",
                    filters=[
                        ComparisonFilter(type="eq", key="scope", value="user"),
                        ComparisonFilter(type="eq", key="user_id", value="user-a"),
                    ],
                ),
                CompoundFilter(
                    type="and",
                    filters=[
                        ComparisonFilter(type="eq", key="scope", value="user_agent"),
                        ComparisonFilter(type="eq", key="user_id", value="user-a"),
                        ComparisonFilter(type="eq", key="agent_id", value="agent-a"),
                    ],
                ),
            ],
        )
        stella_result = await index_a.query_vector(
            np.asarray([1.0, 0.0, 0.0]),
            10,
            0.0,
            stella_filter,
        )
        assert {chunk.chunk_id for chunk in stella_result.chunks} == {
            "system",
            "system-agent-a",
            "user-a",
            "user-agent-a",
        }

        await index_a.delete()
        remaining = await client.count(collection_name=collection_name, exact=True)
        assert remaining.count == 1
        result_b_after_delete = await index_b.query_vector(np.asarray([1.0, 0.0, 0.0]), 10, 0.0)
        assert [chunk.metadata["department_id"] for chunk in result_b_after_delete.chunks] == ["dept-b"]
    finally:
        if await client.collection_exists(collection_name):
            await client.delete_collection(collection_name)
        await client.close()
