"""OGX 外置 Qdrant VectorIO Provider 的注册入口。"""

from __future__ import annotations

from typing import Any

from ogx_api import Api, RemoteProviderSpec
from ogx_api.vector_io import VectorIO

from .adapter import SharedQdrantVectorIOAdapter
from .config import SharedQdrantVectorIOConfig


def get_provider_spec() -> RemoteProviderSpec:
    """返回 OGX 用于发现本 Provider 的稳定规格。"""

    return RemoteProviderSpec(
        api=Api.vector_io,
        adapter_type="shared-qdrant",
        provider_type="remote::shared-qdrant",
        config_class="shared_knowledge_service.provider.config.SharedQdrantVectorIOConfig",
        module="shared_knowledge_service.provider",
        pip_packages=["qdrant-client==1.18.0"],
        api_dependencies=[Api.inference],
        optional_api_dependencies=[Api.files, Api.models, Api.file_processors],
        is_external=True,
        description="Shared Qdrant provider for Stella and Cherry Studio Enterprise.",
    )


async def get_adapter_impl(
    config: SharedQdrantVectorIOConfig,
    deps: dict[Api, Any],
    policy: list[Any] | None = None,
) -> VectorIO:
    """构造使用共享 Collection 的外置 Provider。"""

    impl = SharedQdrantVectorIOAdapter(
        config,
        deps[Api.inference],
        deps.get(Api.files),
        deps.get(Api.file_processors),
        policy=policy or [],
    )
    await impl.initialize()
    return impl


__all__ = ["SharedQdrantVectorIOConfig", "get_adapter_impl", "get_provider_spec"]
