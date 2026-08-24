"""OGX 外置 Provider 注册契约测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from ogx.core.distribution import get_provider_registry
from ogx_api import Api, RemoteProviderSpec

from shared_knowledge_service.provider import get_provider_spec


def test_provider_spec_declares_external_vector_io() -> None:
    spec = get_provider_spec()

    assert spec.api is Api.vector_io
    assert spec.provider_type == "remote::shared-qdrant"
    assert spec.module == "shared_knowledge_service.provider"
    assert spec.is_external is True
    assert spec.api_dependencies == [Api.inference]


def test_ogx_registry_loads_provider_from_project_module() -> None:
    # OGX 的模块加载器只依赖这三个配置属性；用最小对象隔离注册探针与完整运行配置。
    config = SimpleNamespace(
        external_apis_dir=None,
        external_providers_dir=None,
        providers={
            Api.vector_io: [
                SimpleNamespace(
                    module="shared_knowledge_service",
                    provider_type="remote::shared-qdrant",
                )
            ]
        },
    )

    registry = get_provider_registry(cast(Any, config))
    loaded = registry[Api.vector_io]["remote::shared-qdrant"]

    assert isinstance(loaded, RemoteProviderSpec)
    assert loaded.config_class.endswith("SharedQdrantVectorIOConfig")
