"""KnowledgeBase 感知 Inference Provider 的 OGX 注册入口。"""

from __future__ import annotations

from typing import Any

from ogx.core.storage.kvstore import kvstore_impl
from ogx_api import Api, RemoteProviderSpec
from ogx_api.inference import InferenceProvider

from shared_knowledge_service.api.state import CredentialCipher, KnowledgeState
from shared_knowledge_service.api.upstream import InferenceUrlPolicy

from .adapter import KnowledgeBaseInferenceAdapter
from .config import KnowledgeBaseInferenceConfig


def get_provider_spec() -> RemoteProviderSpec:
    """声明一个同时承担 Embedding 与 Rerank 的 KnowledgeBase 感知 Provider。"""

    return RemoteProviderSpec(
        api=Api.inference,
        adapter_type="knowledge-base-inference",
        provider_type="remote::knowledge-base-inference",
        config_class=("shared_knowledge_service.knowledge_base_inference.config.KnowledgeBaseInferenceConfig"),
        module="shared_knowledge_service.knowledge_base_inference.provider",
        pip_packages=[],
        is_external=True,
        description="KnowledgeBase-aware embedding and rerank provider for the shared knowledge service.",
    )


async def get_adapter_impl(
    config: KnowledgeBaseInferenceConfig,
    _deps: dict[Api, Any],
) -> InferenceProvider:
    """使用与 Knowledge API 相同的 PostgreSQL KV namespace 和 Master Key。"""

    kvstore = await kvstore_impl(config.persistence)
    state = KnowledgeState(kvstore, CredentialCipher(config.credential_master_key))
    impl = KnowledgeBaseInferenceAdapter(
        state=state,
        url_policy=InferenceUrlPolicy.from_csv(
            allowed_hosts=config.allowed_hosts,
            http_allowed_hosts=config.http_allowed_hosts,
        ),
        timeout_seconds=config.timeout_seconds,
    )
    await impl.initialize()
    return impl


__all__ = ["KnowledgeBaseInferenceConfig", "get_adapter_impl", "get_provider_spec"]
