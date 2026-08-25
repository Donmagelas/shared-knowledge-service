"""OGX 外置 Provider 注册契约测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml
from ogx.core.configure import parse_and_maybe_upgrade_config
from ogx.core.distribution import get_provider_registry
from ogx.providers.inline.files.localfs.config import LocalfsFilesImplConfig
from ogx.providers.remote.files.s3.config import S3FilesImplConfig
from ogx_api import Api, RemoteProviderSpec

from shared_knowledge_service.provider import get_provider_spec
from shared_knowledge_service.tenant_inference import get_provider_spec as get_tenant_inference_provider_spec


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


def test_tenant_inference_provider_spec_declares_external_inference() -> None:
    spec = get_tenant_inference_provider_spec()

    assert spec.api is Api.inference
    assert spec.provider_type == "remote::tenant-inference"
    assert spec.module == "shared_knowledge_service.tenant_inference.provider"
    assert spec.is_external is True


def test_project_config_registers_tenant_inference_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实项目配置只启用租户感知 Provider，不再使用全局模型连接。"""

    monkeypatch.setenv("KNOWLEDGE_CREDENTIAL_MASTER_KEY", "test-master-key-at-least-sixteen")
    monkeypatch.setenv("KNOWLEDGE_RUNTIME_TOKEN", "runtime-test-at-least-sixteen")
    monkeypatch.setenv("KNOWLEDGE_ADMIN_TOKEN", "admin-test-at-least-sixteen")
    repository_root = Path(__file__).parents[2]
    config = parse_and_maybe_upgrade_config(yaml.safe_load((repository_root / "config/ogx.yaml").read_text()))

    registry = get_provider_registry(config)

    assert "remote::tenant-inference" in registry[Api.inference]


@pytest.mark.parametrize(
    ("provider_type", "config_type"),
    [
        ("inline::localfs", LocalfsFilesImplConfig),
        ("remote::s3", S3FilesImplConfig),
    ],
)
def test_project_config_supports_selectable_file_storage(
    monkeypatch: pytest.MonkeyPatch,
    provider_type: str,
    config_type: type[LocalfsFilesImplConfig] | type[S3FilesImplConfig],
) -> None:
    """原文件后端只改变部署配置，不改变统一 Knowledge API。"""

    monkeypatch.setenv("KNOWLEDGE_CREDENTIAL_MASTER_KEY", "test-master-key-at-least-sixteen")
    monkeypatch.setenv("KNOWLEDGE_RUNTIME_TOKEN", "runtime-test-at-least-sixteen")
    monkeypatch.setenv("KNOWLEDGE_ADMIN_TOKEN", "admin-test-at-least-sixteen")
    monkeypatch.setenv("FILES_PROVIDER_TYPE", provider_type)
    monkeypatch.setenv("S3_BUCKET_NAME", "knowledge-test-bucket")
    repository_root = Path(__file__).parents[2]
    config = parse_and_maybe_upgrade_config(yaml.safe_load((repository_root / "config/ogx.yaml").read_text()))
    files_provider = config.providers["files"][0]

    validated = config_type.model_validate(files_provider.config)

    assert files_provider.provider_type == provider_type
    assert isinstance(validated, config_type)
