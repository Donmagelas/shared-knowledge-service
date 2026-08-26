"""根据 opaque Profile ID 在调用时解析 KnowledgeBase 模型连接。"""

from __future__ import annotations

from typing import Any, cast

from ogx_api import (
    Model,
    OpenAIEmbeddingData,
    OpenAIEmbeddingsRequestWithExtraBody,
    OpenAIEmbeddingsResponse,
    OpenAIEmbeddingUsage,
    RerankData,
    RerankResponse,
)
from ogx_api.inference import (
    InferenceProvider,
    OpenAIChatCompletionContentPartImageParam,
    OpenAIChatCompletionContentPartTextParam,
    RerankRequest,
)

from shared_knowledge_service.api.errors import KnowledgeError
from shared_knowledge_service.api.state import KnowledgeState
from shared_knowledge_service.api.upstream import InferenceUrlPolicy, post_model_request


def _rerank_text(
    item: str | OpenAIChatCompletionContentPartTextParam | OpenAIChatCompletionContentPartImageParam,
) -> str:
    """V1 的远程 Rerank 只接受文本候选。"""

    if isinstance(item, str):
        return item
    if isinstance(item, OpenAIChatCompletionContentPartTextParam):
        return cast(str, item.text)
    raise ValueError("KnowledgeBase Rerank Provider 暂不支持图像候选")


class KnowledgeBaseInferenceAdapter(InferenceProvider):  # type: ignore[misc]
    """一个 Provider 服务全部 KB，连接参数从加密 Profile 动态解析。"""

    def __init__(
        self,
        *,
        state: KnowledgeState,
        url_policy: InferenceUrlPolicy,
        timeout_seconds: float,
    ) -> None:
        self.state = state
        self.url_policy = url_policy
        self.timeout_seconds = timeout_seconds

    async def initialize(self) -> None:
        """Provider 没有启动期网络调用。"""

    async def shutdown(self) -> None:
        """HTTP Client 按请求创建，不持有额外资源。"""

    async def check_model_availability(self, model: str) -> bool:
        """Profile 的精确模型已由 Admin 配置接口探测。"""

        return bool(await self.state.get_embedding_by_profile(model) or await self.state.get_rerank_by_profile(model))

    async def list_provider_model_ids(self) -> list[str]:
        """禁止模型发现；产品也没有可用模型列表接口。"""

        return []

    async def should_refresh_models(self) -> bool:
        """KnowledgeBase 模型只由 Admin API 注册，禁止远程自动刷新。"""

        return False

    async def list_models(self) -> list[Model]:
        """不向 OGX 或产品暴露上游模型目录。"""

        return []

    async def register_model(self, model: Model) -> Model:
        """只接受已经持久化且类型匹配的 opaque Profile ID。"""

        embedding = await self.state.get_embedding_by_profile(model.provider_resource_id)
        rerank = await self.state.get_rerank_by_profile(model.provider_resource_id)
        if embedding is None and rerank is None:
            raise ValueError("KnowledgeBase 模型 Profile 不存在")
        return model

    async def unregister_model(self, model_id: str) -> None:
        """Profile 生命周期由 Knowledge API 管理；Registry 删除无需修改 Provider 状态。"""

        del model_id

    async def openai_embeddings(
        self,
        params: OpenAIEmbeddingsRequestWithExtraBody,
    ) -> OpenAIEmbeddingsResponse:
        profile = await self.state.get_embedding_by_profile(params.model)
        if profile is None:
            raise RuntimeError("Embedding Profile 不存在")
        if params.dimensions is not None and params.dimensions != profile.dimension:
            raise ValueError("Embedding 请求维度与 KnowledgeBase 锁定配置不一致")
        api_key = self.state.decrypt_api_key(profile.credential, profile_id=profile.profile_id)
        payload: dict[str, Any] = {
            "model": profile.model_id,
            "input": params.input,
            "encoding_format": "float",
            "dimensions": profile.dimension,
        }
        if params.user is not None:
            payload["user"] = params.user
        result = await post_model_request(
            policy=self.url_policy,
            base_url=profile.base_url,
            api_key=api_key,
            operation="embeddings",
            payload=payload,
            timeout_seconds=self.timeout_seconds,
        )
        rows = result.get("data")
        if not isinstance(rows, list) or not rows:
            raise KnowledgeError(502, "invalid_embedding_response", "Embedding 服务返回结构不合法")
        data: list[OpenAIEmbeddingData] = []
        for position, row in enumerate(rows):
            if not isinstance(row, dict) or not isinstance(row.get("embedding"), list):
                raise KnowledgeError(502, "invalid_embedding_response", "Embedding 服务返回结构不合法")
            vector = row["embedding"]
            if len(vector) != profile.dimension or not all(isinstance(value, int | float) for value in vector):
                raise KnowledgeError(
                    502,
                    "embedding_dimension_mismatch",
                    "Embedding 服务返回维度与 KnowledgeBase 配置不一致",
                )
            index = row.get("index", position)
            if not isinstance(index, int):
                raise KnowledgeError(502, "invalid_embedding_response", "Embedding 服务返回 index 不合法")
            data.append(OpenAIEmbeddingData(index=index, embedding=[float(value) for value in vector]))
        data.sort(key=lambda item: item.index)
        usage_value = result.get("usage")
        usage: dict[str, Any] = usage_value if isinstance(usage_value, dict) else {}
        prompt_tokens = usage.get("prompt_tokens")
        total_tokens = usage.get("total_tokens")
        return OpenAIEmbeddingsResponse(
            data=data,
            model=profile.model_id,
            usage=OpenAIEmbeddingUsage(
                prompt_tokens=int(prompt_tokens) if isinstance(prompt_tokens, int | float | str) else 0,
                total_tokens=(
                    int(total_tokens)
                    if isinstance(total_tokens, int | float | str)
                    else int(prompt_tokens)
                    if isinstance(prompt_tokens, int | float | str)
                    else 0
                ),
            ),
        )

    async def rerank(self, request: RerankRequest) -> RerankResponse:
        profile = await self.state.get_rerank_by_profile(request.model)
        if profile is None or not profile.enabled:
            raise RuntimeError("Rerank Profile 不存在或未启用")
        if profile.base_url is None or profile.model_id is None or profile.credential is None:
            raise RuntimeError("Rerank Profile 配置不完整")
        api_key = self.state.decrypt_api_key(profile.credential, profile_id=profile.profile_id)
        payload: dict[str, Any] = {
            "model": profile.model_id,
            "query": _rerank_text(request.query),
            "documents": [_rerank_text(item) for item in request.items],
        }
        if request.max_num_results is not None:
            payload["top_n"] = request.max_num_results
        result = await post_model_request(
            policy=self.url_policy,
            base_url=profile.base_url,
            api_key=api_key,
            operation="rerank",
            payload=payload,
            timeout_seconds=self.timeout_seconds,
        )
        rows = result.get("results", result.get("data"))
        if not isinstance(rows, list):
            raise KnowledgeError(502, "invalid_rerank_response", "Rerank 服务返回结构不合法")
        data: list[RerankData] = []
        for row in rows:
            if not isinstance(row, dict):
                raise KnowledgeError(502, "invalid_rerank_response", "Rerank 服务返回结构不合法")
            try:
                item = RerankData(index=int(row["index"]), relevance_score=float(row["relevance_score"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise KnowledgeError(502, "invalid_rerank_response", "Rerank 服务返回结构不合法") from exc
            if item.index < 0 or item.index >= len(request.items):
                raise KnowledgeError(502, "invalid_rerank_response", "Rerank 服务返回越界 index")
            data.append(item)
        data.sort(key=lambda item: item.relevance_score, reverse=True)
        return RerankResponse(data=data)
