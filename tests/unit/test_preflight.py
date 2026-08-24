"""Embedding 前置探针的单元测试。"""

from __future__ import annotations

import httpx
import pytest

from shared_knowledge_service.preflight import EmbeddingProbeConfig, PreflightError, probe_embedding


def _config(**overrides: object) -> EmbeddingProbeConfig:
    values: dict[str, object] = {
        "base_url": "https://embedding.example.test/v1",
        "api_key": "test-secret",
        "model": "test-embedding-model",
        "expected_dimension": 3,
        "batch_size": 2,
        "timeout_seconds": 5.0,
    }
    values.update(overrides)
    return EmbeddingProbeConfig(**values)  # type: ignore[arg-type]


def test_config_reports_missing_names_without_values() -> None:
    with pytest.raises(PreflightError, match="EMBEDDING_API_KEY") as error:
        EmbeddingProbeConfig.from_mapping(
            {
                "EMBEDDING_BASE_URL": "https://embedding.example.test/v1",
                "EMBEDDING_API_KEY": "",
                "EMBEDDING_MODEL": "model-a",
            }
        )

    assert "test-secret" not in str(error.value)


def test_probe_returns_sanitized_summary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer test-secret"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 0, "embedding": [0.1, 0.2, 0.3]},
                    {"index": 1, "embedding": [0.4, 0.5, 0.6]},
                ]
            },
        )

    result = probe_embedding(_config(), transport=httpx.MockTransport(handler))

    assert result["dimension"] == 3
    assert result["returned_vectors"] == 2
    assert result["accepted_batch_size"] == 2
    assert "test-secret" not in str(result)
    assert "embedding.example.test" not in str(result)


def test_probe_does_not_inherit_host_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """开发机代理不得改变真实 Embedding 探针的网络路径。"""

    observed_client_options: dict[str, object] = {}

    class RecordingClient:
        def __init__(self, **kwargs: object) -> None:
            observed_client_options.update(kwargs)

        def __enter__(self) -> RecordingClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"embedding": [0.1, 0.2, 0.3]},
                        {"embedding": [0.4, 0.5, 0.6]},
                    ]
                },
            )

    monkeypatch.setattr(httpx, "Client", RecordingClient)

    probe_embedding(_config())

    assert observed_client_options["trust_env"] is False


def test_probe_rejects_inconsistent_dimensions() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3]}]},
        )
    )

    with pytest.raises(PreflightError, match="维度不一致"):
        probe_embedding(_config(), transport=transport)


def test_probe_rejects_dimension_different_from_configuration() -> None:
    """模型实际维度必须与部署配置一致。"""

    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]},
        )
    )

    with pytest.raises(PreflightError, match="配置 3，实际 2"):
        probe_embedding(_config(), transport=transport)


def test_probe_does_not_echo_remote_error_body() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            401,
            json={"detail": "request carried test-secret to https://internal.example.test"},
        )
    )

    with pytest.raises(PreflightError, match="HTTP 401") as error:
        probe_embedding(_config(), transport=transport)

    assert "test-secret" not in str(error.value)
    assert "internal.example.test" not in str(error.value)
