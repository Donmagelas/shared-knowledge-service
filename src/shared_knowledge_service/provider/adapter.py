"""把 OGX VectorStore 生命周期桥接到共享 Qdrant Collection。"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

import numpy as np
from ogx.core.storage.kvstore import kvstore_impl
from ogx.log import get_logger
from ogx.providers.remote.vector_io.qdrant.qdrant import VECTOR_DBS_PREFIX, QdrantVectorIOAdapter
from ogx.providers.utils.inference.prompt_adapter import interleaved_content_as_str
from ogx.providers.utils.memory.vector_store import VectorStoreWithIndex
from ogx_api import (
    ComparisonFilter,
    CompoundFilter,
    OpenAICreateVectorStoreRequestWithExtraBody,
    OpenAIEmbeddingsRequestWithExtraBody,
    OpenAIUpdateVectorStoreRequest,
    QueryChunksResponse,
    VectorStore,
    VectorStoreDeleteResponse,
    VectorStoreFileBatchObject,
    VectorStoreNotFoundError,
    VectorStoreObject,
)
from ogx_api.inference import RerankRequest
from qdrant_client import AsyncQdrantClient

from .config import SharedQdrantVectorIOConfig
from .index import SharedQdrantIndex

log = get_logger(name=__name__, category="vector_io::shared_qdrant")
TENANT_METADATA_KEY = "tenant_id"
DENSE_VECTOR_METADATA_KEY = "dense_vector_name"
_IMMUTABLE_METADATA_KEYS = frozenset(
    {
        TENANT_METADATA_KEY,
        "embedding_dimension",
        "embedding_model",
        DENSE_VECTOR_METADATA_KEY,
        "provider_id",
        "provider_vector_store_id",
    }
)


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

    @staticmethod
    def _tenant_id_from_metadata(metadata: dict[str, Any] | None) -> str | None:
        """读取用于存储路由的可信租户 ID；缺失表示单租户默认 Collection。"""

        if not metadata or TENANT_METADATA_KEY not in metadata:
            return None
        value = metadata[TENANT_METADATA_KEY]
        if not isinstance(value, str):
            raise ValueError("VectorStore metadata.tenant_id 必须是字符串")
        normalized = value.strip()
        if not normalized:
            raise ValueError("VectorStore metadata.tenant_id 不能为空")
        if len(normalized) > 256:
            raise ValueError("VectorStore metadata.tenant_id 不能超过 256 个字符")
        return normalized

    @staticmethod
    def _dense_vector_name_from_metadata(metadata: dict[str, Any] | None) -> str:
        if not metadata:
            raise ValueError("VectorStore metadata 缺少 dense_vector_name")
        value = metadata.get(DENSE_VECTOR_METADATA_KEY)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("VectorStore metadata.dense_vector_name 必须是非空字符串")
        return value.strip()

    async def _bind_cached_store(
        self,
        cached: VectorStoreWithIndex,
        tenant_id: str | None,
        dense_vector_name: str,
    ) -> str:
        if not isinstance(cached.index, SharedQdrantIndex):
            raise RuntimeError("逻辑知识库未使用 SharedQdrantIndex")
        collection_name = self.config.collection_name_for_tenant(tenant_id)
        cached.index.bind_storage(collection_name, dense_vector_name)
        return collection_name

    async def _ensure_cached_store_route(self, vector_store_id: str, cached: VectorStoreWithIndex) -> str:
        store_info = self.openai_vector_stores.get(vector_store_id)
        if store_info is None:
            raise VectorStoreNotFoundError(vector_store_id)
        metadata = store_info.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("VectorStore metadata 必须是对象")
        tenant_id = self._tenant_id_from_metadata(metadata)
        dense_name = self._dense_vector_name_from_metadata(metadata)
        return await self._bind_cached_store(cached, tenant_id, dense_name)

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
            self.cache[vector_store.identifier] = VectorStoreWithIndex(
                vector_store,
                shared_index,
                self.inference_api,
            )
        await self.initialize_openai_vector_stores()
        for vector_store_id, cached in self.cache.items():
            await self._ensure_cached_store_route(vector_store_id, cached)

    async def shutdown(self) -> None:
        """优雅停机时保留未完成 Batch，供单实例重启后自动恢复。"""

        resumable_batch_ids = {
            batch_id
            for batch_id, batch_info in self.openai_file_batches.items()
            if batch_info.get("status") == "in_progress"
        }
        await super().shutdown()

        # OGX 原生 shutdown 会把后台 Task 的 CancelledError 持久化成 cancelled。
        # 服务停机不等于用户取消任务，因此把本次停机前仍在处理的 Batch 恢复为 in_progress。
        for batch_id in resumable_batch_ids:
            batch_info = self.openai_file_batches.get(batch_id)
            if batch_info is None or batch_info.get("status") != "cancelled":
                continue
            batch_info["status"] = "in_progress"
            await self._save_openai_vector_store_file_batch(batch_id, batch_info)

    async def register_vector_store(self, vector_store: VectorStore) -> None:
        if self.kvstore is None:
            raise RuntimeError("KVStore 尚未初始化")
        await self.kvstore.set(
            key=f"{VECTOR_DBS_PREFIX}{vector_store.identifier}",
            value=vector_store.model_dump_json(),
        )
        shared_index = self._new_index(vector_store)
        self.cache[vector_store.identifier] = VectorStoreWithIndex(
            vector_store=vector_store,
            index=shared_index,
            inference_api=self.inference_api,
        )

    async def openai_create_vector_store(
        self,
        params: OpenAICreateVectorStoreRequestWithExtraBody,
    ) -> VectorStoreObject:
        """只绑定租户路由；空 KnowledgeBase 不提前创建物理 Collection。"""

        extra = params.model_extra or {}
        vector_store_id = extra.get("provider_vector_store_id")
        if not isinstance(vector_store_id, str) or not vector_store_id:
            raise ValueError("OGX 没有向 Provider 传入 provider_vector_store_id")
        cached = self.cache.get(vector_store_id)
        if cached is None:
            raise VectorStoreNotFoundError(vector_store_id)
        tenant_id = self._tenant_id_from_metadata(params.metadata)
        dense_name = self._dense_vector_name_from_metadata(params.metadata)
        await self._bind_cached_store(cached, tenant_id, dense_name)
        return await super().openai_create_vector_store(params)

    async def ensure_vector_store_collection(self, vector_store_id: str) -> str:
        """首个 Ingest 接受前显式创建或校验租户 Collection。"""

        cached = await self._get_and_cache_vector_store_index(vector_store_id)
        if cached is None or not isinstance(cached.index, SharedQdrantIndex):
            raise VectorStoreNotFoundError(vector_store_id)
        await cached.index.initialize()
        return cached.index.bound_collection_name

    def find_single_file_batch(
        self,
        vector_store_id: str,
        file_id: str,
        attributes: dict[str, Any],
        *,
        excluded_batch_ids: set[str] | None = None,
    ) -> VectorStoreFileBatchObject | None:
        """恢复“Batch 已持久化、Knowledge 状态尚未落盘”的 Ingest。

        OGX 的 Batch API 不接受调用方预分配 ID；因此统一接口重放时按
        ``VectorStore + 单个 File + attributes`` 找回已经创建的唯一 Batch，
        避免进程在两个持久化步骤之间退出后重复创建后台任务。
        """

        matches = [
            batch_info
            for batch_info in self.openai_file_batches.values()
            if batch_info.get("vector_store_id") == vector_store_id
            and batch_info.get("file_ids") == [file_id]
            and (batch_info.get("attributes") or {}) == attributes
            and str(batch_info.get("id", "")) not in (excluded_batch_ids or set())
        ]
        if not matches:
            return None
        # 正常情况下只有一个；若历史版本曾产生重复，固定复用最早的 Batch，
        # 不在重放过程中再扩大重复任务。
        earliest = min(matches, key=lambda item: (int(item.get("created_at", 0)), str(item.get("id", ""))))
        return VectorStoreFileBatchObject.model_validate(earliest)

    def has_in_progress_file_batch(self, vector_store_id: str, file_id: str) -> bool:
        """判断未完成 Ingest 是否仍有 OGX Batch 正在处理该 File。"""

        return any(
            batch_info.get("vector_store_id") == vector_store_id
            and file_id in (batch_info.get("file_ids") or [])
            and batch_info.get("status") == "in_progress"
            for batch_info in self.openai_file_batches.values()
        )

    async def reconfigure_empty_vector_store(
        self,
        vector_store_id: str,
        *,
        embedding_model: str,
        embedding_dimension: int,
        dense_vector_name: str,
    ) -> None:
        """首次 Ingest 前只更新目标 KB，不再连带修改同租户其他知识库。"""

        store_info = self.openai_vector_stores.get(vector_store_id)
        if store_info is None:
            raise VectorStoreNotFoundError(vector_store_id)
        counts = store_info.get("file_counts") or {}
        if counts.get("total", 0) != 0:
            raise RuntimeError("已有文件的 VectorStore 不能修改 Embedding 模型或维度")
        metadata = dict(store_info.get("metadata") or {})
        metadata["embedding_model"] = embedding_model
        metadata["embedding_dimension"] = str(embedding_dimension)
        metadata[DENSE_VECTOR_METADATA_KEY] = dense_vector_name
        store_info["metadata"] = metadata
        await self._save_openai_vector_store(vector_store_id, store_info)
        cached = self.cache.get(vector_store_id)
        if cached is None:
            cached = await self._get_and_cache_vector_store_index(vector_store_id)
        if cached is None or not isinstance(cached.index, SharedQdrantIndex):
            raise RuntimeError("逻辑知识库未使用 SharedQdrantIndex")
        cached.vector_store.embedding_model = embedding_model
        cached.vector_store.embedding_dimension = embedding_dimension
        cached.index.reconfigure_empty_vector_space(dense_vector_name, embedding_dimension)
        if self.kvstore is None:
            raise RuntimeError("KVStore 尚未初始化")
        await self.kvstore.set(
            key=f"{VECTOR_DBS_PREFIX}{vector_store_id}",
            value=cached.vector_store.model_dump_json(),
        )

    async def openai_update_vector_store(
        self,
        vector_store_id: str,
        request: OpenAIUpdateVectorStoreRequest,
    ) -> VectorStoreObject:
        """允许修改业务 metadata，但禁止变更已确定的租户和索引配置。"""

        if request.metadata is None:
            return await super().openai_update_vector_store(vector_store_id, request)

        current = self.openai_vector_stores.get(vector_store_id)
        if current is None:
            raise VectorStoreNotFoundError(vector_store_id)
        current_metadata = dict(current.get("metadata") or {})
        incoming_metadata = dict(request.metadata)
        for key in _IMMUTABLE_METADATA_KEYS:
            # OGX RoutingTable 注册时会先生成一份不含租户字段的空 metadata；
            # 允许 Knowledge API 在任何文件接收前补齐一次，之后保持不可变。
            if key in current_metadata and key in incoming_metadata and incoming_metadata[key] != current_metadata[key]:
                raise ValueError(f"VectorStore metadata.{key} 创建后不能修改")
        merged_metadata = {**current_metadata, **incoming_metadata}
        updated_request = request.model_copy(update={"metadata": merged_metadata})
        return await super().openai_update_vector_store(vector_store_id, updated_request)

    async def openai_delete_vector_store(self, vector_store_id: str) -> VectorStoreDeleteResponse:
        """先可靠删除 scoped Point，再让 OGX 删除对象 metadata。

        OGX 基类会吞掉底层 ``unregister_vector_store`` 异常；若直接复用，
        可能出现 metadata 已删除但 Qdrant Point 仍残留且无法重试。这里先执行
        可幂等的 scoped delete，成功后才进入基类对象清理。
        """

        cached = await self._get_and_cache_vector_store_index(vector_store_id)
        if cached is None:
            raise VectorStoreNotFoundError(vector_store_id)
        await self._ensure_cached_store_route(vector_store_id, cached)
        await cached.index.delete()
        return await super().openai_delete_vector_store(vector_store_id)

    async def unregister_vector_store(self, vector_store_id: str) -> None:
        cached = self.cache.pop(vector_store_id, None)
        if cached is not None:
            await cached.index.delete()
        if self.kvstore is None:
            raise RuntimeError("KVStore 尚未初始化")
        await self.kvstore.delete(f"{VECTOR_DBS_PREFIX}{vector_store_id}")

    async def _get_and_cache_vector_store_index(self, vector_store_id: str) -> VectorStoreWithIndex | None:
        if vector_store_id in self.cache:
            cached = self.cache[vector_store_id]
            await self._ensure_cached_store_route(vector_store_id, cached)
            return cached
        if self.kvstore is None:
            raise RuntimeError("KVStore 尚未初始化")
        vector_store_data = await self.kvstore.get(f"{VECTOR_DBS_PREFIX}{vector_store_id}")
        if not vector_store_data:
            raise VectorStoreNotFoundError(vector_store_id)
        vector_store = VectorStore.model_validate_json(vector_store_data)
        shared_index = self._new_index(vector_store)
        cached = VectorStoreWithIndex(vector_store, shared_index, self.inference_api)
        self.cache[vector_store_id] = cached
        await self._ensure_cached_store_route(vector_store_id, cached)
        return cached

    async def query_vector_store(
        self,
        vector_store_id: str,
        query: str,
        mode: Literal["hybrid", "dense", "bm25"],
        limit: int,
        filters: ComparisonFilter | CompoundFilter | None = None,
        rerank_model: str | None = None,
    ) -> QueryChunksResponse:
        """按单个 KnowledgeBase 的模型、Named Vector 和 Payload 范围完成本地排序。"""

        cached = await self._get_and_cache_vector_store_index(vector_store_id)
        if cached is None:
            raise VectorStoreNotFoundError(vector_store_id)
        if not isinstance(cached.index, SharedQdrantIndex):
            raise RuntimeError("逻辑知识库未使用 SharedQdrantIndex")

        if mode == "bm25":
            return await cached.index.query_keyword(query, limit, 0.0, filters)

        embeddings = await self.inference_api.openai_embeddings(
            OpenAIEmbeddingsRequestWithExtraBody(
                model=cached.vector_store.embedding_model,
                input=[query],
                dimensions=cached.vector_store.embedding_dimension,
            )
        )
        query_vector = np.asarray(embeddings.data[0].embedding, dtype=np.float32)
        if mode == "dense":
            return await cached.index.query_vector(query_vector, limit, 0.0, filters)
        candidate_limit = max(limit, self.config.rerank_candidate_limit) if rerank_model is not None else limit
        candidates = await cached.index.query_hybrid(
            query_vector,
            query,
            candidate_limit,
            0.0,
            "rrf",
            filters=filters,
        )
        return await self._apply_optional_rerank(query, candidates, limit, rerank_model=rerank_model)

    async def _apply_optional_rerank(
        self,
        query: str,
        candidates: QueryChunksResponse,
        limit: int,
        rerank_model: str | None = None,
    ) -> QueryChunksResponse:
        """按部署开关重排 Hybrid 候选；远程失败时保留 RRF 结果。"""

        fallback = QueryChunksResponse(
            chunks=candidates.chunks[:limit],
            scores=candidates.scores[:limit],
        )
        if rerank_model is None or not candidates.chunks:
            return fallback

        try:
            response = await self.inference_api.rerank(
                RerankRequest(
                    model=rerank_model,
                    query=query,
                    items=[interleaved_content_as_str(chunk.content) for chunk in candidates.chunks],
                    max_num_results=limit,
                )
            )
        except Exception as exc:
            # Rerank 是可选的效果增强层；上游故障不应让基础检索整体不可用。
            log.error(
                "Remote reranking failed; returning Qdrant RRF results",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return fallback

        reranked_chunks = []
        reranked_scores = []
        used_indexes: set[int] = set()
        for item in response.data:
            if item.index in used_indexes or item.index >= len(candidates.chunks):
                continue
            used_indexes.add(item.index)
            reranked_chunks.append(candidates.chunks[item.index])
            reranked_scores.append(item.relevance_score)
            if len(reranked_chunks) == limit:
                break

        if not reranked_chunks:
            log.error("Remote reranking returned no valid candidate indexes; returning Qdrant RRF results")
            return fallback
        return QueryChunksResponse(chunks=reranked_chunks, scores=reranked_scores)
