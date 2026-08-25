"""OGX 外置 Provider 注册契约测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml
from ogx.core.configure import parse_and_maybe_upgrade_config
from ogx.core.distribution import get_provider_registry
from ogx_api import Api, RemoteProviderSpec

from shared_knowledge_service.provider import get_provider_spec
from shared_knowledge_service.rerank import get_provider_spec as get_rerank_provider_spec


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


def test_rerank_provider_spec_declares_external_inference() -> None:
    spec = get_rerank_provider_spec()

    assert spec.api is Api.inference
    assert spec.provider_type == "remote::shared-rerank"
    assert spec.module == "shared_knowledge_service.rerank.provider"
    assert spec.is_external is True


def test_ogx_registry_loads_rerank_provider_from_project_module() -> None:
    config = SimpleNamespace(
        external_apis_dir=None,
        external_providers_dir=None,
        providers={
            Api.inference: [
                SimpleNamespace(
                    module="shared_knowledge_service.rerank",
                    provider_type="remote::shared-rerank",
                )
            ]
        },
    )

    registry = get_provider_registry(cast(Any, config))
    loaded = registry[Api.inference]["remote::shared-rerank"]

    assert isinstance(loaded, RemoteProviderSpec)
    assert loaded.config_class.endswith("VersionedRerankConfig")


def test_project_config_registers_external_rerank_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """使用真实项目配置验证外置 Provider，而不是依赖 OGX 1.3.0 的 dry-run 缺陷路径。"""

    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://embedding.example/v1")
    repository_root = Path(__file__).parents[2]
    config = parse_and_maybe_upgrade_config(yaml.safe_load((repository_root / "config/ogx.yaml").read_text()))

    registry = get_provider_registry(config)

    assert "remote::shared-rerank" in registry[Api.inference]
