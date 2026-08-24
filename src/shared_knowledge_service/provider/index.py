"""共享 Qdrant Collection 中的逻辑 VectorStore 索引实现。"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any

from numpy.typing import NDArray
from ogx.providers.utils.inference.prompt_adapter import interleaved_content_as_str
from ogx.providers.utils.memory.vector_store import EmbeddingIndex
from ogx.providers.utils.vector_io.vector_utils import load_embedded_chunk_with_backward_compat
from ogx_api import (
    ChunkForDeletion,
    ComparisonFilter,
    CompoundFilter,
    EmbeddedChunk,
    QueryChunksResponse,
    VectorStore,
)
from qdrant_client import AsyncQdrantClient, models

from .bm25 import native_bm25_document
from .config import PayloadIndexType, SharedQdrantVectorIOConfig
from .filtering import RESERVED_ATTRIBUTE_FIELDS, scoped_filter


def compound_point_id(vector_store_id: str, chunk_id: str) -> str:
    """生成包含逻辑知识库范围的稳定 Qdrant UUID。"""

    digest = hashlib.sha256(f"shared-qdrant:{vector_store_id}:{chunk_id}".encode()).hexdigest()
    return str(uuid.UUID(digest[:32]))


def _payload_schema(index_type: PayloadIndexType) -> models.PayloadSchemaType:
    return {
        PayloadIndexType.KEYWORD: models.PayloadSchemaType.KEYWORD,
        PayloadIndexType.INTEGER: models.PayloadSchemaType.INTEGER,
        PayloadIndexType.FLOAT: models.PayloadSchemaType.FLOAT,
        PayloadIndexType.BOOL: models.PayloadSchemaType.BOOL,
        PayloadIndexType.DATETIME: models.PayloadSchemaType.DATETIME,
        PayloadIndexType.TEXT: models.PayloadSchemaType.TEXT,
    }[index_type]


class SharedQdrantIndex(EmbeddingIndex):  # type: ignore[misc]
    """一个物理 Collection 内由 ``vector_store_id`` 隔离的逻辑索引。"""

    def __init__(
        self,
        client: AsyncQdrantClient,
        vector_store: VectorStore,
        config: SharedQdrantVectorIOConfig,
        collection_lock: asyncio.Lock,
    ) -> None:
        self.client = client
        self.vector_store_id = vector_store.identifier
        self.dimension = vector_store.embedding_dimension
        self.config = config
        self.collection_lock = collection_lock

    async def initialize(self) -> None:
        """按首个 VectorStore 的维度创建 Collection，后续注册只校验并复用。"""

        async with self.collection_lock:
            if not await self.client.collection_exists(self.config.collection_name):
                await self.client.create_collection(
                    collection_name=self.config.collection_name,
                    vectors_config={
                        self.config.dense_vector_name: models.VectorParams(
                            size=self.dimension,
                            distance=models.Distance.COSINE,
                        )
                    },
                    sparse_vectors_config={
                        self.config.sparse_vector_name: models.SparseVectorParams(modifier=models.Modifier.IDF)
                    },
                )
            await self._validate_collection_schema()
            await self._ensure_payload_indexes()

    async def _validate_collection_schema(self) -> None:
        info = await self.client.get_collection(self.config.collection_name)
        vectors = info.config.params.vectors
        sparse_vectors = info.config.params.sparse_vectors
        if not isinstance(vectors, dict) or self.config.dense_vector_name not in vectors:
            raise RuntimeError(f"Qdrant Collection 缺少 Dense Vector：{self.config.dense_vector_name}")
        if vectors[self.config.dense_vector_name].size != self.dimension:
            raise RuntimeError(
                f"Qdrant Collection Dense 维度为 {vectors[self.config.dense_vector_name].size}，"
                f"但 VectorStore 请求 {self.dimension}"
            )
        if not isinstance(sparse_vectors, dict) or self.config.sparse_vector_name not in sparse_vectors:
            raise RuntimeError(f"Qdrant Collection 缺少 Sparse Vector：{self.config.sparse_vector_name}")
        if sparse_vectors[self.config.sparse_vector_name].modifier is not models.Modifier.IDF:
            raise RuntimeError("Qdrant BM25 Sparse Vector 必须启用动态 IDF")

    async def _ensure_payload_indexes(self) -> None:
        info = await self.client.get_collection(self.config.collection_name)
        declared: dict[str, PayloadIndexType] = {
            "vector_store_id": PayloadIndexType.KEYWORD,
            "file_id": PayloadIndexType.KEYWORD,
            "chunk_id": PayloadIndexType.KEYWORD,
        }
        declared.update({f"attributes.{key}": value for key, value in self.config.payload_indexes.items()})
        for field_name, index_type in declared.items():
            if field_name in info.payload_schema:
                continue
            await self.client.create_payload_index(
                collection_name=self.config.collection_name,
                field_name=field_name,
                field_schema=_payload_schema(index_type),
                wait=True,
            )

    def _payload_for_chunk(self, chunk: EmbeddedChunk) -> dict[str, Any]:
        metadata = dict(chunk.metadata or {})
        forbidden = RESERVED_ATTRIBUTE_FIELDS.intersection(metadata)
        if forbidden:
            joined = ", ".join(sorted(forbidden))
            raise ValueError(f"Chunk attributes 不能覆盖保留字段：{joined}")

        file_id = metadata.pop("file_id", None) or metadata.get("document_id")
        if not file_id and chunk.chunk_metadata:
            file_id = chunk.chunk_metadata.document_id
        return {
            "vector_store_id": self.vector_store_id,
            "file_id": str(file_id or ""),
            "chunk_id": chunk.chunk_id,
            "attributes": metadata,
            "content_text": interleaved_content_as_str(chunk.content),
            "chunk_content": chunk.model_dump(mode="json"),
        }

    async def add_chunks(self, embedded_chunks: list[EmbeddedChunk]) -> None:
        if not embedded_chunks:
            return
        points: list[models.PointStruct] = []
        for chunk in embedded_chunks:
            payload = self._payload_for_chunk(chunk)
            vectors: dict[str, models.Vector] = {self.config.dense_vector_name: chunk.embedding}
            sparse_vector = native_bm25_document(payload["content_text"])
            if sparse_vector is not None:
                vectors[self.config.sparse_vector_name] = sparse_vector
            points.append(
                models.PointStruct(
                    id=compound_point_id(self.vector_store_id, chunk.chunk_id),
                    vector=vectors,
                    payload=payload,
                )
            )
        await self.client.upsert(collection_name=self.config.collection_name, points=points, wait=True)

    async def delete_chunks(self, chunks_for_deletion: list[ChunkForDeletion]) -> None:
        if not chunks_for_deletion:
            return
        await self.client.delete(
            collection_name=self.config.collection_name,
            points_selector=models.PointIdsList(
                points=[compound_point_id(self.vector_store_id, chunk.chunk_id) for chunk in chunks_for_deletion]
            ),
            wait=True,
        )

    async def query_vector(
        self,
        embedding: NDArray[Any],
        k: int,
        score_threshold: float,
        filters: ComparisonFilter | CompoundFilter | None = None,
    ) -> QueryChunksResponse:
        return await self.query_vector_scoped(
            [self.vector_store_id],
            embedding,
            k,
            score_threshold,
            filters,
        )

    async def query_vector_scoped(
        self,
        vector_store_ids: list[str],
        embedding: NDArray[Any],
        k: int,
        score_threshold: float,
        filters: ComparisonFilter | CompoundFilter | None = None,
    ) -> QueryChunksResponse:
        """在一次 Qdrant 查询中检索一个或多个逻辑 VectorStore。"""

        results = (
            await self.client.query_points(
                collection_name=self.config.collection_name,
                query=embedding.tolist(),
                using=self.config.dense_vector_name,
                query_filter=scoped_filter(vector_store_ids, filters, self.config.payload_indexes),
                limit=k,
                with_payload=True,
                score_threshold=score_threshold,
            )
        ).points
        return self._query_response(results)

    async def query_keyword(
        self,
        query_string: str,
        k: int,
        score_threshold: float,
        filters: ComparisonFilter | CompoundFilter | None = None,
    ) -> QueryChunksResponse:
        return await self.query_keyword_scoped(
            [self.vector_store_id],
            query_string,
            k,
            score_threshold,
            filters,
        )

    async def query_keyword_scoped(
        self,
        vector_store_ids: list[str],
        query_string: str,
        k: int,
        score_threshold: float,
        filters: ComparisonFilter | CompoundFilter | None = None,
    ) -> QueryChunksResponse:
        """在一次 Qdrant 查询中执行跨逻辑 VectorStore 的 BM25。"""

        sparse_query = native_bm25_document(query_string)
        if sparse_query is None:
            return QueryChunksResponse(chunks=[], scores=[])
        results = (
            await self.client.query_points(
                collection_name=self.config.collection_name,
                query=sparse_query,
                using=self.config.sparse_vector_name,
                query_filter=scoped_filter(vector_store_ids, filters, self.config.payload_indexes),
                limit=k,
                with_payload=True,
                score_threshold=score_threshold,
            )
        ).points
        return self._query_response(results)

    async def query_hybrid(
        self,
        embedding: NDArray[Any],
        query_string: str,
        k: int,
        score_threshold: float,
        reranker_type: str,
        reranker_params: dict[str, Any] | None = None,
        filters: ComparisonFilter | CompoundFilter | None = None,
    ) -> QueryChunksResponse:
        return await self.query_hybrid_scoped(
            [self.vector_store_id],
            embedding,
            query_string,
            k,
            score_threshold,
            reranker_type,
            reranker_params,
            filters,
        )

    async def query_hybrid_scoped(
        self,
        vector_store_ids: list[str],
        embedding: NDArray[Any],
        query_string: str,
        k: int,
        score_threshold: float,
        reranker_type: str,
        reranker_params: dict[str, Any] | None = None,
        filters: ComparisonFilter | CompoundFilter | None = None,
    ) -> QueryChunksResponse:
        """在一次 Qdrant Query API 调用中完成跨逻辑库 Dense + BM25 + RRF。"""

        if reranker_type != "rrf":
            raise NotImplementedError(f"MVP 混合检索只支持 RRF，不支持 {reranker_type}")

        sparse_query = native_bm25_document(query_string)
        if sparse_query is None:
            return await self.query_vector_scoped(vector_store_ids, embedding, k, score_threshold, filters)

        query_filter = scoped_filter(vector_store_ids, filters, self.config.payload_indexes)
        candidate_limit = max(k * 4, 20)
        results = (
            await self.client.query_points(
                collection_name=self.config.collection_name,
                prefetch=[
                    models.Prefetch(
                        query=embedding.tolist(),
                        using=self.config.dense_vector_name,
                        filter=query_filter,
                        limit=candidate_limit,
                    ),
                    models.Prefetch(
                        query=sparse_query,
                        using=self.config.sparse_vector_name,
                        filter=query_filter,
                        limit=candidate_limit,
                    ),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=k,
                with_payload=True,
                score_threshold=score_threshold,
            )
        ).points
        return self._query_response(results)

    def _query_response(self, points: list[models.ScoredPoint]) -> QueryChunksResponse:
        chunks: list[EmbeddedChunk] = []
        scores: list[float] = []
        for point in points:
            if point.payload is None:
                raise RuntimeError("Qdrant 查询返回了不含 Payload 的 Point")
            chunks.append(load_embedded_chunk_with_backward_compat(point.payload["chunk_content"]))
            scores.append(point.score)
        return QueryChunksResponse(chunks=chunks, scores=scores)

    async def delete(self) -> None:
        """删除逻辑 VectorStore 的 Point，不删除共享 Collection。"""

        await self.client.delete(
            collection_name=self.config.collection_name,
            points_selector=models.FilterSelector(filter=scoped_filter(self.vector_store_id)),
            wait=True,
        )
