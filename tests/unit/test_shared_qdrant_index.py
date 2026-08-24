"""共享 Qdrant 索引的数据组织单元测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import numpy as np
import pytest
from ogx.core.storage.datatypes import KVStoreReference
from ogx_api import ChunkMetadata, EmbeddedChunk, VectorStore
from qdrant_client import AsyncQdrantClient, models

from shared_knowledge_service.provider.config import SharedQdrantVectorIOConfig
from shared_knowledge_service.provider.index import SharedQdrantIndex, compound_point_id


def _vector_store(identifier: str) -> VectorStore:
    return VectorStore(
        identifier=identifier,
        provider_id="shared-qdrant",
        embedding_model="test-model",
        embedding_dimension=3,
    )


def _index(identifier: str) -> SharedQdrantIndex:
    # 这些测试不访问 Client；cast 只用于构造被测索引并检查纯数据转换。
    client = cast(AsyncQdrantClient, object())
    config = SharedQdrantVectorIOConfig(
        url="http://qdrant.test",
        persistence=KVStoreReference(backend="test", namespace="test"),
    )
    return SharedQdrantIndex(client, _vector_store(identifier), config, asyncio.Lock())


def _chunk(metadata: dict[str, object] | None = None) -> EmbeddedChunk:
    return EmbeddedChunk(
        content="产品 A 的说明文档",
        chunk_id="same-chunk-id",
        metadata=metadata or {"file_id": "file-a", "department_id": "dept-a"},
        chunk_metadata=ChunkMetadata(document_id="file-a"),
        embedding=[1.0, 0.0, 0.0],
        embedding_model="test-model",
        embedding_dimension=3,
    )


def test_point_id_is_stable_but_scoped_by_vector_store() -> None:
    assert compound_point_id("vs-a", "chunk-a") == compound_point_id("vs-a", "chunk-a")
    assert compound_point_id("vs-a", "chunk-a") != compound_point_id("vs-b", "chunk-a")


def test_payload_separates_service_fields_from_business_attributes() -> None:
    payload = _index("vs-a")._payload_for_chunk(_chunk())

    assert payload["vector_store_id"] == "vs-a"
    assert payload["file_id"] == "file-a"
    assert payload["chunk_id"] == "same-chunk-id"
    assert payload["attributes"] == {"department_id": "dept-a"}
    assert payload["content_text"] == "产品 A 的说明文档"


def test_business_metadata_cannot_overwrite_reserved_payload_fields() -> None:
    with pytest.raises(ValueError, match="保留字段"):
        _index("vs-a")._payload_for_chunk(_chunk({"vector_store_id": "vs-b"}))


@pytest.mark.asyncio
async def test_multi_store_hybrid_search_is_one_qdrant_query() -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def query_points(self, **kwargs: object) -> SimpleNamespace:
            self.calls.append(kwargs)
            return SimpleNamespace(points=[])

    client = RecordingClient()
    index = _index("vs-a")
    index.client = cast(AsyncQdrantClient, client)

    await index.query_hybrid_scoped(
        ["vs-a", "vs-b"],
        np.asarray([1.0, 0.0, 0.0]),
        "退款材料",
        10,
        0.0,
        "rrf",
    )

    assert len(client.calls) == 1
    prefetch = cast(list[models.Prefetch], client.calls[0]["prefetch"])
    query_filter = cast(models.Filter, prefetch[0].filter)
    scope = cast(models.FieldCondition, query_filter.must[0])
    assert scope.match == models.MatchAny(any=["vs-a", "vs-b"])
