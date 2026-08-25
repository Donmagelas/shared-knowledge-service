"""远程 Rerank Provider 配置。"""

from __future__ import annotations

from ogx.providers.remote.inference.vllm.config import VLLMInferenceAdapterConfig
from pydantic import Field


class VersionedRerankConfig(VLLMInferenceAdapterConfig):  # type: ignore[misc]
    """扩展 OGX vLLM 配置，使 Provider 可以被部署开关完全禁用。"""

    enabled: bool = Field(
        default=False,
        description="是否允许该 Provider 发起远程 Rerank 请求",
    )
