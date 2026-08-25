"""OGX 外置远程 Rerank Provider 的注册入口。"""

from __future__ import annotations

from typing import Any

from ogx_api import Api, RemoteProviderSpec
from ogx_api.inference import InferenceProvider

from .adapter import VersionedRerankInferenceAdapter
from .config import VersionedRerankConfig


def get_provider_spec() -> RemoteProviderSpec:
    """返回 OGX 用于发现本 Provider 的稳定规格。"""

    return RemoteProviderSpec(
        api=Api.inference,
        adapter_type="shared-rerank",
        provider_type="remote::shared-rerank",
        config_class="shared_knowledge_service.rerank.config.VersionedRerankConfig",
        module="shared_knowledge_service.rerank.provider",
        pip_packages=[],
        provider_data_validator="ogx.providers.remote.inference.vllm.VLLMProviderDataValidator",
        is_external=True,
        description="Version-aware remote rerank provider for the shared knowledge service.",
    )


async def get_adapter_impl(
    config: VersionedRerankConfig,
    _deps: dict[Api, Any],
) -> InferenceProvider:
    """构造只负责远程 Rerank 的 Inference Provider。"""

    impl = VersionedRerankInferenceAdapter(config=config)
    await impl.initialize()
    return impl


__all__ = ["VersionedRerankConfig", "get_adapter_impl", "get_provider_spec"]
