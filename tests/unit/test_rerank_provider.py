"""远程 Rerank Provider 的协议与开关测试。"""

from __future__ import annotations

import json

import httpx
import pytest
from ogx_api.inference import RerankRequest

from shared_knowledge_service.rerank.adapter import VersionedRerankInferenceAdapter
from shared_knowledge_service.rerank.config import VersionedRerankConfig
from shared_knowledge_service.rerank.provider import get_provider_spec


@pytest.mark.asyncio
async def test_versioned_rerank_provider_calls_v1_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.95},
                    {"index": 0, "relevance_score": 0.10},
                ]
            },
        )

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(
        VersionedRerankInferenceAdapter,
        "_build_httpx_client_kwargs",
        lambda _self: {"transport": transport},
    )
    adapter = VersionedRerankInferenceAdapter(
        config=VersionedRerankConfig(
            enabled=True,
            base_url="https://rerank.example/v1",
            api_token="test-secret",
        )
    )
    # OGX Resolver 在真实启动时注入这两个属性；单元测试显式复原同一运行条件。
    adapter.__provider_id__ = "rerank"
    adapter.__provider_spec__ = get_provider_spec()

    # Rerank Provider 只使用 OGX 静态注册的模型，不能把同一网关里的 Chat/Embedding 模型导入进来。
    assert await adapter.list_provider_model_ids() == []

    response = await adapter.rerank(
        RerankRequest(
            model="qwen/qwen3-reranker-0.6b",
            query="退款材料",
            items=["食堂菜单", "订单号和付款凭证"],
            max_num_results=2,
        )
    )

    assert [item.index for item in response.data] == [1, 0]
    assert len(requests) == 1
    request = requests[0]
    assert request.url == "https://rerank.example/v1/rerank"
    assert request.headers["Authorization"] == "Bearer test-secret"
    assert json.loads(request.content) == {
        "model": "qwen/qwen3-reranker-0.6b",
        "query": "退款材料",
        "documents": ["食堂菜单", "订单号和付款凭证"],
        "top_n": 2,
    }


@pytest.mark.asyncio
async def test_disabled_rerank_provider_does_not_require_remote_endpoint() -> None:
    adapter = VersionedRerankInferenceAdapter(config=VersionedRerankConfig(enabled=False))

    await adapter.initialize()
    assert await adapter.check_model_availability("qwen/qwen3-reranker-0.6b") is True
    with pytest.raises(RuntimeError, match="未启用"):
        await adapter.rerank(
            RerankRequest(
                model="qwen/qwen3-reranker-0.6b",
                query="退款材料",
                items=["订单号和付款凭证"],
            )
        )
