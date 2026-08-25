"""Hybrid Search 可选 Rerank 行为测试。"""

from __future__ import annotations

from typing import Any

import pytest
from ogx.core.storage.datatypes import KVStoreReference
from ogx_api import ChunkMetadata, EmbeddedChunk, QueryChunksResponse, RerankData, RerankResponse

from shared_knowledge_service.provider.adapter import SharedQdrantVectorIOAdapter
from shared_knowledge_service.provider.config import SharedQdrantVectorIOConfig


def _chunk(chunk_id: str, content: str) -> EmbeddedChunk:
    return EmbeddedChunk(
        content=content,
        chunk_id=chunk_id,
        metadata={"file_id": f"file-{chunk_id}"},
        chunk_metadata=ChunkMetadata(document_id=f"file-{chunk_id}"),
        embedding=[1.0, 0.0, 0.0],
        embedding_model="embedding/test",
        embedding_dimension=3,
    )


def _candidates() -> QueryChunksResponse:
    return QueryChunksResponse(
        chunks=[
            _chunk("first", "公司食堂每周五供应面条。"),
            _chunk("second", "退款需要订单号和付款凭证。"),
        ],
        scores=[0.8, 0.7],
    )


class FakeInference:
    """记录 OGX Rerank 请求并返回确定性重排结果。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.requests: list[Any] = []

    async def rerank(self, request: Any) -> RerankResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return RerankResponse(
            data=[
                RerankData(index=1, relevance_score=0.99),
                RerankData(index=0, relevance_score=0.01),
            ]
        )


class TestAdapter(SharedQdrantVectorIOAdapter):
    """绕过真实 Qdrant 初始化，只测试可选重排步骤。"""

    __test__ = False

    def __init__(self, config: SharedQdrantVectorIOConfig, inference: FakeInference) -> None:
        self.config = config
        self.inference_api = inference


def _config(*, enabled: bool) -> SharedQdrantVectorIOConfig:
    del enabled
    return SharedQdrantVectorIOConfig(
        url="http://qdrant.test",
        persistence=KVStoreReference(backend="test", namespace="test"),
    )


@pytest.mark.asyncio
async def test_enabled_rerank_reorders_candidates_and_replaces_scores() -> None:
    inference = FakeInference()
    adapter = TestAdapter(_config(enabled=True), inference)

    result = await adapter._apply_optional_rerank(
        "退款材料",
        _candidates(),
        2,
        rerank_model="tenant-inference/rerank-tenant-a",
    )

    assert [chunk.chunk_id for chunk in result.chunks] == ["second", "first"]
    assert result.scores == [0.99, 0.01]
    assert len(inference.requests) == 1
    assert inference.requests[0].model == "tenant-inference/rerank-tenant-a"
    assert inference.requests[0].max_num_results == 2


@pytest.mark.asyncio
async def test_disabled_rerank_returns_rrf_results_without_remote_call() -> None:
    inference = FakeInference()
    adapter = TestAdapter(_config(enabled=False), inference)

    result = await adapter._apply_optional_rerank("退款材料", _candidates(), 1, rerank_model=None)

    assert [chunk.chunk_id for chunk in result.chunks] == ["first"]
    assert result.scores == [0.8]
    assert inference.requests == []


@pytest.mark.asyncio
async def test_rerank_failure_falls_back_to_rrf_results() -> None:
    inference = FakeInference(error=TimeoutError("upstream timeout"))
    adapter = TestAdapter(_config(enabled=True), inference)

    result = await adapter._apply_optional_rerank(
        "退款材料",
        _candidates(),
        1,
        rerank_model="tenant-inference/rerank-tenant-a",
    )

    assert [chunk.chunk_id for chunk in result.chunks] == ["first"]
    assert result.scores == [0.8]
