"""把 OGX VectorStore 生命周期桥接到共享 Qdrant Collection。"""

from __future__ import annotations

import asyncio
from typing import Literal

import numpy as np
from ogx.core.storage.kvstore import kvstore_impl
from ogx.providers.remote.vector_io.qdrant.qdrant import VECTOR_DBS_PREFIX, QdrantVectorIOAdapter
from ogx.providers.utils.memory.vector_store import VectorStoreWithIndex
from ogx_api import (
    ComparisonFilter,
    CompoundFilter,
    OpenAIEmbeddingsRequestWithExtraBody,
    QueryChunksResponse,
    VectorStore,
    VectorStoreNotFoundError,
)
from qdrant_client import AsyncQdrantClient

from .config import SharedQdrantVectorIOConfig
from .index import SharedQdrantIndex


class SharedQdrantVectorIOAdapter(QdrantVectorIOAdapter):  # type: ignore[misc]
    """复用 OGX 对象与任务能力，只替换物理索引组织和检索实现。"""

    config: SharedQdrantVectorIOConfig

    def __init__(self, config: SharedQdrantVectorIOConfig, *args: object, **kwargs: object) -> None:
        super().__init__(config, *args, **kwargs)
        self.config = config
        self._shared_collection_lock = asyncio.Lock()

    def _new_index(self, vector_store: VectorStore) -> SharedQdrantIndex:
        return SharedQdrantIndex(
            client=self.client,
            vector_store=vector_store,
            config=self.config,
            collection_lock=self._shared_collection_lock,
        )

    async def initialize(self) -> None:
        self.client = AsyncQdrantClient(**self.config.qdrant_client_kwargs())
        self.kvstore = await kvstore_impl(self.config.persistence)

        if self.config.metadata_store:
            from ogx.core.storage.sqlstore import authorized_sqlstore

            self.metadata_store = await authorized_sqlstore(self.config.metadata_store, self._policy)

        stored_vector_stores = await self.kvstore.values_in_range(
            VECTOR_DBS_PREFIX,
            f"{VECTOR_DBS_PREFIX}\xff",
        )
        for vector_store_data in stored_vector_stores:
            vector_store = VectorStore.model_validate_json(vector_store_data)
            shared_index = self._new_index(vector_store)
            await shared_index.initialize()
            self.cache[vector_store.identifier] = VectorStoreWithIndex(
                vector_store,
                shared_index,
                self.inference_api,
            )
        await self.initialize_openai_vector_stores()

    async def register_vector_store(self, vector_store: VectorStore) -> None:
        if self.kvstore is None:
            raise RuntimeError("KVStore 尚未初始化")
        await self.kvstore.set(
            key=f"{VECTOR_DBS_PREFIX}{vector_store.identifier}",
            value=vector_store.model_dump_json(),
        )
        shared_index = self._new_index(vector_store)
        await shared_index.initialize()
        self.cache[vector_store.identifier] = VectorStoreWithIndex(
            vector_store=vector_store,
            index=shared_index,
            inference_api=self.inference_api,
        )

    async def unregister_vector_store(self, vector_store_id: str) -> None:
        cached = self.cache.pop(vector_store_id, None)
        if cached is not None:
            await cached.index.delete()
        if self.kvstore is None:
            raise RuntimeError("KVStore 尚未初始化")
        await self.kvstore.delete(f"{VECTOR_DBS_PREFIX}{vector_store_id}")

    async def _get_and_cache_vector_store_index(self, vector_store_id: str) -> VectorStoreWithIndex | None:
        if vector_store_id in self.cache:
            return self.cache[vector_store_id]
        if self.kvstore is None:
            raise RuntimeError("KVStore 尚未初始化")
        vector_store_data = await self.kvstore.get(f"{VECTOR_DBS_PREFIX}{vector_store_id}")
        if not vector_store_data:
            raise VectorStoreNotFoundError(vector_store_id)
        vector_store = VectorStore.model_validate_json(vector_store_data)
        shared_index = self._new_index(vector_store)
        await shared_index.initialize()
        cached = VectorStoreWithIndex(vector_store, shared_index, self.inference_api)
        self.cache[vector_store_id] = cached
        return cached

    async def query_multiple_vector_stores(
        self,
        vector_store_ids: list[str],
        query: str,
        mode: Literal["hybrid", "dense", "bm25"],
        limit: int,
        filters: ComparisonFilter | CompoundFilter | None = None,
    ) -> QueryChunksResponse:
        """用一次 Qdrant 查询检索同一 Collection 中的多个逻辑 VectorStore。"""

        if not vector_store_ids:
            raise ValueError("vector_store_ids 不能为空")

        cached_stores: list[VectorStoreWithIndex] = []
        for vector_store_id in vector_store_ids:
            cached = await self._get_and_cache_vector_store_index(vector_store_id)
            if cached is None:
                raise VectorStoreNotFoundError(vector_store_id)
            cached_stores.append(cached)

        first = cached_stores[0]
        expected_model = first.vector_store.embedding_model
        expected_dimension = first.vector_store.embedding_dimension
        if any(
            cached.vector_store.embedding_model != expected_model
            or cached.vector_store.embedding_dimension != expected_dimension
            for cached in cached_stores[1:]
        ):
            raise ValueError("一次检索中的 knowledge_base_ids 必须使用同一 Embedding 模型和维度")
        if not isinstance(first.index, SharedQdrantIndex):
            raise RuntimeError("逻辑知识库未使用 SharedQdrantIndex")

        if mode == "bm25":
            return await first.index.query_keyword_scoped(vector_store_ids, query, limit, 0.0, filters)

        embeddings = await self.inference_api.openai_embeddings(
            OpenAIEmbeddingsRequestWithExtraBody(
                model=expected_model,
                input=[query],
                dimensions=expected_dimension,
            )
        )
        query_vector = np.asarray(embeddings.data[0].embedding, dtype=np.float32)
        if mode == "dense":
            return await first.index.query_vector_scoped(vector_store_ids, query_vector, limit, 0.0, filters)
        return await first.index.query_hybrid_scoped(
            vector_store_ids,
            query_vector,
            query,
            limit,
            0.0,
            "rrf",
            filters=filters,
        )
