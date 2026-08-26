"""共享 Qdrant 索引的数据组织单元测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest
from ogx.core.storage.datatypes import KVStoreReference
from ogx_api import ChunkMetadata, EmbeddedChunk, OpenAIUpdateVectorStoreRequest, VectorStore
from qdrant_client import AsyncQdrantClient, models

from shared_knowledge_service.provider.adapter import SharedQdrantVectorIOAdapter
from shared_knowledge_service.provider.config import SharedQdrantVectorIOConfig, dense_vector_name
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
    return SharedQdrantIndex(
        client,
        _vector_store(identifier),
        config,
        asyncio.Lock(),
        collection_name=config.collection_name,
        dense_vector_name=dense_vector_name("test-model", 3),
    )


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


def test_tenant_collection_name_is_stable_and_does_not_expose_tenant_id() -> None:
    config = _index("vs-a").config

    tenant_a = config.collection_name_for_tenant("company-a")
    tenant_b = config.collection_name_for_tenant("company-b")

    assert tenant_a == config.collection_name_for_tenant("company-a")
    assert tenant_a != tenant_b
    assert "company-a" not in tenant_a
    assert config.collection_name_for_tenant(None) == config.collection_name


def test_storage_binding_can_replace_placeholders_before_initialize() -> None:
    index = _index("vs-a")

    index.bind_storage(index.config.collection_name, dense_vector_name("test-model", 3))
    index.bind_storage("another-tenant", dense_vector_name("another-model", 4))

    assert index.bound_collection_name == "another-tenant"
    assert index.dense_vector_name == dense_vector_name("another-model", 4)


def test_bound_storage_cannot_be_changed_after_initialize() -> None:
    index = _index("vs-a")

    index.bind_storage(index.config.collection_name, dense_vector_name("test-model", 3))
    index._initialized = True
    with pytest.raises(ValueError, match="不能迁移"):
        index.bind_storage("another-tenant", dense_vector_name("another-model", 4))


def test_dense_vector_name_is_stable_and_separates_vector_spaces() -> None:
    assert dense_vector_name("embedding/model-a", 1024) == dense_vector_name("embedding/model-a", 1024)
    assert dense_vector_name("embedding/model-a", 1024) != dense_vector_name("embedding/model-a", 768)
    assert dense_vector_name("embedding/model-a", 1024) != dense_vector_name("embedding/model-b", 1024)


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
async def test_single_store_hybrid_search_uses_its_named_vector_and_payload_scope() -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def query_points(self, **kwargs: object) -> SimpleNamespace:
            self.calls.append(kwargs)
            return SimpleNamespace(points=[])

    client = RecordingClient()
    index = _index("vs-a")
    index.client = cast(AsyncQdrantClient, client)
    # Collection 初始化由其他测试和集成测试覆盖；本测试只记录一次查询调用。
    index._initialized = True

    await index.query_hybrid(
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
    assert prefetch[0].using == dense_vector_name("test-model", 3)
    assert scope.match == models.MatchValue(value="vs-a")


@pytest.mark.asyncio
async def test_adapter_rejects_tenant_route_change() -> None:
    config = SharedQdrantVectorIOConfig(
        url="http://qdrant.test",
        persistence=KVStoreReference(backend="test", namespace="test"),
    )
    adapter = SharedQdrantVectorIOAdapter(config, cast(Any, object()))
    adapter.openai_vector_stores = {
        "vs-a": {
            "metadata": {
                "tenant_id": "tenant-a",
                "embedding_model": "test-model",
                "embedding_dimension": "3",
                "dense_vector_name": dense_vector_name("test-model", 3),
            }
        }
    }

    with pytest.raises(ValueError, match="tenant_id.*不能修改"):
        await adapter.openai_update_vector_store(
            "vs-a",
            OpenAIUpdateVectorStoreRequest(metadata={"tenant_id": "tenant-b"}),
        )
