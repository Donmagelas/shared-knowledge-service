"""适配带版本前缀的 Jina-compatible Rerank Endpoint。"""

from __future__ import annotations

from ogx.providers.remote.inference.vllm.vllm import VLLMInferenceAdapter
from ogx_api import RerankResponse
from ogx_api.inference import RerankRequest

from .config import VersionedRerankConfig


class VersionedRerankInferenceAdapter(VLLMInferenceAdapter):  # type: ignore[misc]
    """复用 OGX vLLM Rerank 协议，同时保留 Base URL 中的 ``/v1``。

    OGX v1.3.0 的 vLLM Provider 会主动移除 Base URL 末尾的 ``/v1``，
    但当前使用的 OpenAI-compatible 网关只在 ``/v1/rerank`` 暴露接口。
    本 Provider 只注册 Rerank 模型，不承担 Chat、Embedding 或 Anthropic 请求。
    """

    config: VersionedRerankConfig

    async def initialize(self) -> None:
        """关闭时不要求远程地址可用，也不产生网络访问。"""

        if not self.config.enabled:
            return
        await super().initialize()

    async def check_model_availability(self, model: str) -> bool:
        """关闭时允许 OGX 完成静态模型注册，真正调用仍会被拒绝。"""

        if not self.config.enabled:
            return True
        return bool(await super().check_model_availability(model))

    async def list_provider_model_ids(self) -> list[str]:
        """只使用 OGX 配置中的静态 Rerank 模型，不导入网关里的其他模型。"""

        # 同一个推理网关通常还会暴露 Chat 与 Embedding 模型；动态发现会把它们错误注册到本 Provider。
        return []

    def _get_base_url_without_version(self) -> str:
        """保留部署配置中的版本路径，由父类继续拼接 ``/rerank``。"""

        return str(self.get_base_url()).rstrip("/")

    async def rerank(self, request: RerankRequest) -> RerankResponse:
        """执行远程 Rerank；关闭时给出明确错误，防止绕过部署开关。"""

        if not self.config.enabled:
            raise RuntimeError("远程 Rerank Provider 未启用")
        return await super().rerank(request)
