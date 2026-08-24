"""Qdrant 前置探针配置的单元测试。"""

from __future__ import annotations

import pytest

from shared_knowledge_service.qdrant_preflight import QdrantPreflightError, QdrantProbeConfig


def test_qdrant_config_uses_local_default() -> None:
    config = QdrantProbeConfig.from_mapping({})

    assert config.url == "http://localhost:6333"
    assert config.api_key is None
    assert config.timeout_seconds == 30


@pytest.mark.parametrize("raw_timeout", ["0", "-1", "invalid"])
def test_qdrant_config_rejects_invalid_timeout(raw_timeout: str) -> None:
    with pytest.raises(QdrantPreflightError, match="正整数"):
        QdrantProbeConfig.from_mapping({"QDRANT_TIMEOUT_SECONDS": raw_timeout})
