"""按 Stella 与 Cherry Studio 企业版实际调用方式验证统一 Knowledge API。"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = [pytest.mark.e2e]

_RUNTIME_TOKEN = "runtime-e2e-at-least-sixteen"
_ADMIN_TOKEN = "admin-e2e-at-least-sixteen"


def _base_url() -> str:
    value = os.environ.get("KNOWLEDGE_E2E_URL")
    if not value:
        pytest.skip("未设置 KNOWLEDGE_E2E_URL")
    return value


def _runtime_headers(**extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_RUNTIME_TOKEN}", **extra}


def _admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_ADMIN_TOKEN}"}


def _fixture_bytes() -> bytes:
    return (Path(__file__).parents[1] / "fixtures" / "knowledge.md").read_bytes()


def _create_knowledge_base(
    client: httpx.Client,
    tenant_id: str,
    *,
    key: str | None = None,
    embedding_key: str = "embedding-secret",
    embedding_model: str = "deterministic-test",
    dimension: int | None = 3,
    rerank: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按正式契约一次创建 KB 及其独立 Embedding/可选 Rerank 配置。"""

    embedding: dict[str, Any] = {
        "base_url": "http://embedding-stub:18080/v1",
        "api_key": embedding_key,
        "model_id": embedding_model,
    }
    if dimension is not None:
        embedding["dimension"] = dimension
    response = client.post(
        "/knowledge/v1/knowledge-bases",
        headers=_runtime_headers(**{"Idempotency-Key": key or f"create-{uuid.uuid4()}"}),
        json={"tenant_id": tenant_id, "embedding": embedding, "rerank": rerank},
    )
    response.raise_for_status()
    assert response.status_code in {200, 201}
    assert embedding_key not in response.text
    if rerank is not None:
        assert rerank["api_key"] not in response.text
    return response.json()


def _submit_ingest(
    client: httpx.Client,
    knowledge_base_id: str,
    *,
    key: str,
    filename: str = "knowledge.md",
    attributes: dict[str, Any] | None = None,
) -> httpx.Response:
    return client.post(
        "/knowledge/v1/ingest",
        headers=_runtime_headers(**{"Idempotency-Key": key}),
        data={
            "knowledge_base_id": knowledge_base_id,
            "attributes": json.dumps(attributes or {}, ensure_ascii=False),
        },
        files={"file": (filename, _fixture_bytes(), "text/markdown")},
    )


def _wait_operation(
    client: httpx.Client,
    operation_id: str,
    *,
    timeout: float = 90,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(
            f"/knowledge/v1/operations/{operation_id}",
            headers=_runtime_headers(),
        )
        response.raise_for_status()
        body = response.json()
        if body["status"] != "processing":
            return body
        time.sleep(0.1)
    raise TimeoutError(f"Operation {operation_id} 在 {timeout} 秒内未完成")


def _delete_knowledge_base(client: httpx.Client, knowledge_base_id: str) -> None:
    response = client.delete(
        f"/knowledge/v1/knowledge-bases/{knowledge_base_id}",
        headers=_runtime_headers(),
    )
    assert response.status_code == 204, response.text


def _set_embedding_stub_failure(enabled: bool) -> None:
    """用 E2E Stub 的临时文件注入 Embedding 503，不进入生产配置。"""

    repository_root = Path(__file__).parents[2]
    compose = ["docker", "compose", "-f", "compose.yaml", "-f", "compose.e2e.yaml"]
    container_id = subprocess.check_output(
        [*compose, "ps", "-q", "embedding-stub"],
        cwd=repository_root,
        text=True,
        timeout=10,
    ).strip()
    if not container_id:
        raise RuntimeError("Embedding Stub 服务不存在")
    command = ["touch", "/tmp/embedding_stub.fail"] if enabled else ["rm", "-f", "/tmp/embedding_stub.fail"]
    subprocess.run(["docker", "exec", container_id, *command], check=True, timeout=10)


def test_authentication_and_credential_endpoints_do_not_leak_keys() -> None:
    tenant_id = f"auth-{uuid.uuid4().hex[:10]}"
    knowledge_base_id: str | None = None
    with httpx.Client(base_url=_base_url(), timeout=30, trust_env=False) as client:
        created = _create_knowledge_base(
            client,
            tenant_id,
            embedding_key="tenant-embedding-private",
            dimension=None,
        )
        knowledge_base_id = str(created["knowledge_base_id"])
        assert created["embedding"]["dimension"] == 3

        path = f"/knowledge/v1/knowledge-bases/{knowledge_base_id}/inference-config"
        unauthenticated = client.get(path)
        runtime_only = client.get(
            path,
            headers=_runtime_headers(),
        )

        assert unauthenticated.status_code == 401
        assert unauthenticated.json()["error"]["code"] == "unauthorized"
        assert runtime_only.status_code == 403
        assert runtime_only.json()["error"]["code"] == "admin_token_required"

        configured = client.get(
            path,
            headers=_admin_headers(),
        )
        configured.raise_for_status()
        assert "tenant-embedding-private" not in configured.text
        assert configured.json()["knowledge_base_id"] == knowledge_base_id
        assert set(configured.json()["embedding"]) == {
            "base_url",
            "model_id",
            "dimension",
            "credential_configured",
            "locked",
            "updated_at",
        }
        _delete_knowledge_base(client, knowledge_base_id)
        knowledge_base_id = None
    if knowledge_base_id is not None:
        with httpx.Client(base_url=_base_url(), timeout=30, trust_env=False) as cleanup_client:
            _delete_knowledge_base(cleanup_client, knowledge_base_id)


def test_openapi_contains_only_confirmed_public_knowledge_endpoints_and_raw_api_is_admin_only() -> None:
    with httpx.Client(base_url=_base_url(), timeout=30, trust_env=False) as client:
        schema = client.get("/openapi.json", headers=_admin_headers())
        schema.raise_for_status()
        paths = set(schema.json()["paths"])
        expected = {
            "/knowledge/v1/knowledge-bases",
            "/knowledge/v1/knowledge-bases/{knowledge_base_id}",
            "/knowledge/v1/knowledge-bases/{knowledge_base_id}/inference-config",
            "/knowledge/v1/knowledge-bases/{knowledge_base_id}/embedding-config",
            "/knowledge/v1/knowledge-bases/{knowledge_base_id}/rerank-config",
            "/knowledge/v1/ingest",
            "/knowledge/v1/operations/{operation_id}",
            "/knowledge/v1/operations/{operation_id}/retry",
            "/knowledge/v1/knowledge-bases/{knowledge_base_id}/files/query",
            "/knowledge/v1/knowledge-bases/{knowledge_base_id}/files/{file_id}",
            "/knowledge/v1/search",
        }
        assert expected <= paths
        assert "/knowledge/v1/knowledge-bases/{knowledge_base_id}/operations/{operation_id}" not in paths
        assert "/knowledge/v1/knowledge-bases/{knowledge_base_id}/operations/{operation_id}/retry" not in paths
        assert "/knowledge/v1/internal/auth/validate" not in paths
        assert not any(path.startswith("/knowledge/v1/tenants/") for path in paths)
        assert not any(
            path.startswith(prefix)
            for prefix in ("/v1/agents", "/v1/responses", "/v1/messages", "/v1/tools", "/v1/evals")
            for path in paths
        )

        raw_runtime = client.get("/v1/files", headers=_runtime_headers())
        raw_admin = client.get("/v1/files", headers=_admin_headers())
        assert raw_runtime.status_code == 401
        assert raw_admin.status_code == 200


def test_product_openapi_and_scalar_page_expose_only_stable_knowledge_contract() -> None:
    """调用方无需 Admin Token 即可打开文档，但真实产品接口仍保持独立鉴权。"""

    with httpx.Client(base_url=_base_url(), timeout=30, trust_env=False) as client:
        product_schema = client.get("/knowledge-openapi.json")
        scalar_page = client.get("/api-docs")

        product_schema.raise_for_status()
        scalar_page.raise_for_status()
        schema = product_schema.json()
        assert len(schema["paths"]) == 11
        assert all(path.startswith("/knowledge/v1/") for path in schema["paths"])
        assert not any("/internal/" in path for path in schema["paths"])
        assert schema["components"]["securitySchemes"]["ServiceToken"]["scheme"] == "bearer"
        assert schema["paths"]["/knowledge/v1/ingest"]["post"]["requestBody"]["content"].keys() == {
            "multipart/form-data"
        }
        assert "/knowledge-openapi.json" in scalar_page.text
        assert "@scalar/api-reference@1.66.1" in scalar_page.text


def test_full_product_contract_idempotency_filters_files_rerank_and_delete() -> None:
    tenant_id = f"contract-{uuid.uuid4().hex[:10]}"
    create_key = f"create-{uuid.uuid4()}"
    ingest_key = f"ingest-{uuid.uuid4()}"
    knowledge_base_id: str | None = None
    with httpx.Client(base_url=_base_url(), timeout=120, trust_env=False) as client:
        created = _create_knowledge_base(client, tenant_id, key=create_key)
        knowledge_base_id = str(created["knowledge_base_id"])
        replayed_create = _create_knowledge_base(client, tenant_id, key=create_key)
        assert replayed_create["knowledge_base_id"] == knowledge_base_id

        attributes = {"department_id": "product-a", "scope_ids": ["company", "product-a"]}
        submitted = _submit_ingest(
            client,
            knowledge_base_id,
            key=ingest_key,
            attributes=attributes,
        )
        submitted.raise_for_status()
        assert submitted.status_code == 202
        accepted = submitted.json()
        completed = _wait_operation(client, str(accepted["operation_id"]))
        assert completed["status"] == "completed", completed
        assert completed["retryable"] is False

        replayed = _submit_ingest(
            client,
            knowledge_base_id,
            key=ingest_key,
            attributes=attributes,
        )
        replayed.raise_for_status()
        assert replayed.status_code == 200
        assert replayed.json()["operation_id"] == accepted["operation_id"]
        assert replayed.json()["file_id"] == accepted["file_id"]

        conflict = _submit_ingest(
            client,
            knowledge_base_id,
            key=ingest_key,
            filename="different-name.md",
            attributes=attributes,
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"

        recalled_content: str | None = None
        for mode in ("bm25", "hybrid"):
            result = client.post(
                "/knowledge/v1/search",
                headers=_runtime_headers(),
                json={
                    "query": "退款申请需要什么材料",
                    "knowledge_base_ids": [knowledge_base_id],
                    "mode": mode,
                    "filters": {"type": "eq", "key": "scope_ids", "value": "company"},
                },
            )
            result.raise_for_status()
            assert result.json()["hits"], mode
            assert result.json()["hits"][0]["attributes"] == attributes
            recalled_content = result.json()["hits"][0]["content"]

        # 确定性 Stub 只是文本哈希而非语义模型；用文档原文验证 Dense 协议，
        # 避免把随机余弦是否大于零误当作服务检索效果。
        assert recalled_content is not None
        dense = client.post(
            "/knowledge/v1/search",
            headers=_runtime_headers(),
            json={
                "query": recalled_content,
                "knowledge_base_ids": [knowledge_base_id],
                "mode": "dense",
                "filters": {"type": "eq", "key": "scope_ids", "value": "company"},
            },
        )
        dense.raise_for_status()
        assert dense.json()["hits"]

        filtered_out = client.post(
            "/knowledge/v1/search",
            headers=_runtime_headers(),
            json={
                "query": "退款",
                "knowledge_base_ids": [knowledge_base_id],
                "filters": {"type": "eq", "key": "department_id", "value": "product-b"},
            },
        )
        filtered_out.raise_for_status()
        assert filtered_out.json()["hits"] == []

        files = client.post(
            f"/knowledge/v1/knowledge-bases/{knowledge_base_id}/files/query",
            headers=_runtime_headers(),
            json={"filters": {"type": "eq", "key": "department_id", "value": "product-a"}},
        )
        files.raise_for_status()
        assert [item["file_id"] for item in files.json()["items"]] == [accepted["file_id"]]
        detail = client.get(
            f"/knowledge/v1/knowledge-bases/{knowledge_base_id}/files/{accepted['file_id']}",
            headers=_runtime_headers(),
        )
        detail.raise_for_status()
        assert detail.json()["knowledge_base_id"] == knowledge_base_id

        rerank_config = client.put(
            f"/knowledge/v1/knowledge-bases/{knowledge_base_id}/rerank-config",
            headers=_admin_headers(),
            json={
                "enabled": True,
                "base_url": "http://embedding-stub:18080/v1",
                "api_key": "tenant-rerank-private",
                "model_id": "deterministic-rerank",
            },
        )
        rerank_config.raise_for_status()
        assert "tenant-rerank-private" not in rerank_config.text
        reranked = client.post(
            "/knowledge/v1/search",
            headers=_runtime_headers(),
            json={"query": "退款", "knowledge_base_ids": [knowledge_base_id], "mode": "hybrid"},
        )
        reranked.raise_for_status()
        assert reranked.json()["hits"][0]["score"] == 1000.0

        rotated = client.put(
            f"/knowledge/v1/knowledge-bases/{knowledge_base_id}/embedding-config",
            headers=_admin_headers(),
            json={
                "base_url": "http://embedding-stub:18080/v1",
                "api_key": "rotated-embedding-private",
                "model_id": "deterministic-test",
                "dimension": 3,
            },
        )
        rotated.raise_for_status()
        assert rotated.json()["locked"] is True
        forbidden_model_change = client.put(
            f"/knowledge/v1/knowledge-bases/{knowledge_base_id}/embedding-config",
            headers=_admin_headers(),
            json={
                "base_url": "http://embedding-stub:18080/v1",
                "api_key": "rotated-embedding-private",
                "model_id": "another-model",
                "dimension": 3,
            },
        )
        assert forbidden_model_change.status_code == 409
        assert forbidden_model_change.json()["error"]["code"] == "embedding_config_locked"

        delete_file = client.delete(
            f"/knowledge/v1/knowledge-bases/{knowledge_base_id}/files/{accepted['file_id']}",
            headers=_runtime_headers(),
        )
        assert delete_file.status_code == 204, delete_file.text
        repeat_delete_file = client.delete(
            f"/knowledge/v1/knowledge-bases/{knowledge_base_id}/files/{accepted['file_id']}",
            headers=_runtime_headers(),
        )
        assert repeat_delete_file.status_code == 204
        assert (
            client.get(
                f"/knowledge/v1/knowledge-bases/{knowledge_base_id}/files/{accepted['file_id']}",
                headers=_runtime_headers(),
            ).status_code
            == 404
        )

        _delete_knowledge_base(client, knowledge_base_id)
        _delete_knowledge_base(client, knowledge_base_id)
        assert (
            client.get(
                f"/knowledge/v1/knowledge-bases/{knowledge_base_id}",
                headers=_runtime_headers(),
            ).status_code
            == 404
        )
        knowledge_base_id = None
    if knowledge_base_id is not None:
        with httpx.Client(base_url=_base_url(), timeout=30, trust_env=False) as cleanup_client:
            _delete_knowledge_base(cleanup_client, knowledge_base_id)


def test_same_tenant_multi_knowledge_base_and_cross_tenant_rejection() -> None:
    tenant_a = f"tenant-a-{uuid.uuid4().hex[:8]}"
    tenant_b = f"tenant-b-{uuid.uuid4().hex[:8]}"
    knowledge_base_ids: list[str] = []
    with httpx.Client(base_url=_base_url(), timeout=120, trust_env=False) as client:
        first = str(
            _create_knowledge_base(
                client,
                tenant_a,
                embedding_key="embedding-key-a",
                embedding_model="keyed-embedding-a",
                dimension=3,
                rerank={
                    "base_url": "http://embedding-stub:18080/v1",
                    "api_key": "rerank-key-a",
                    "model_id": "keyed-rerank-a",
                },
            )["knowledge_base_id"]
        )
        second = str(
            _create_knowledge_base(
                client,
                tenant_a,
                embedding_key="embedding-key-b",
                embedding_model="keyed-embedding-b",
                dimension=5,
                rerank={
                    "base_url": "http://embedding-stub:18080/v1",
                    "api_key": "rerank-key-b",
                    "model_id": "keyed-rerank-b",
                },
            )["knowledge_base_id"]
        )
        other = str(_create_knowledge_base(client, tenant_b, embedding_key="tenant-b-key")["knowledge_base_id"])
        knowledge_base_ids.extend([first, second, other])

        for knowledge_base_id, department in ((first, "company"), (second, "product-a"), (other, "other")):
            accepted = _submit_ingest(
                client,
                knowledge_base_id,
                key=f"ingest-{uuid.uuid4()}",
                filename=f"{department}.md",
                attributes={"department_id": department},
            )
            accepted.raise_for_status()
            terminal = _wait_operation(client, accepted.json()["operation_id"])
            assert terminal["status"] == "completed", terminal

        # 两个单 KB 请求分别经过自己的 Embedding 与 Rerank 凭证；测试桩会拒绝串用 Key。
        for knowledge_base_id in (first, second):
            local = client.post(
                "/knowledge/v1/search",
                headers=_runtime_headers(),
                json={"query": "退款", "knowledge_base_ids": [knowledge_base_id], "mode": "hybrid"},
            )
            local.raise_for_status()
            assert local.json()["hits"][0]["score"] == 1000.0

        same_tenant = client.post(
            "/knowledge/v1/search",
            headers=_runtime_headers(),
            json={"query": "退款", "knowledge_base_ids": [first, second], "mode": "hybrid", "limit": 10},
        )
        same_tenant.raise_for_status()
        assert {hit["knowledge_base_id"] for hit in same_tenant.json()["hits"]} == {first, second}
        assert all(0 < hit["score"] < 1 for hit in same_tenant.json()["hits"])

        cross_tenant = client.post(
            "/knowledge/v1/search",
            headers=_runtime_headers(),
            json={"query": "退款", "knowledge_base_ids": [first, other]},
        )
        assert cross_tenant.status_code == 422
        assert cross_tenant.json()["error"]["code"] == "cross_tenant_search"

        for knowledge_base_id in knowledge_base_ids:
            _delete_knowledge_base(client, knowledge_base_id)
        knowledge_base_ids.clear()
    if knowledge_base_ids:
        with httpx.Client(base_url=_base_url(), timeout=30, trust_env=False) as cleanup_client:
            for knowledge_base_id in knowledge_base_ids:
                _delete_knowledge_base(cleanup_client, knowledge_base_id)


def test_empty_kb_model_updates_lock_after_ingest_and_rerank_can_be_disabled() -> None:
    tenant_id = f"lifecycle-{uuid.uuid4().hex[:10]}"
    knowledge_base_id: str | None = None
    with httpx.Client(base_url=_base_url(), timeout=120, trust_env=False) as client:
        created = _create_knowledge_base(client, tenant_id, dimension=3)
        knowledge_base_id = str(created["knowledge_base_id"])

        changed = client.put(
            f"/knowledge/v1/knowledge-bases/{knowledge_base_id}/embedding-config",
            headers=_admin_headers(),
            json={
                "base_url": "http://embedding-stub:18080/v1",
                "api_key": "embedding-v2-secret",
                "model_id": "deterministic-v2",
                "dimension": 5,
            },
        )
        changed.raise_for_status()
        assert changed.json()["model_id"] == "deterministic-v2"
        assert changed.json()["dimension"] == 5
        assert changed.json()["locked"] is False
        assert "embedding-v2-secret" not in changed.text

        enabled = client.put(
            f"/knowledge/v1/knowledge-bases/{knowledge_base_id}/rerank-config",
            headers=_admin_headers(),
            json={
                "enabled": True,
                "base_url": "http://embedding-stub:18080/v1",
                "api_key": "rerank-v2-secret",
                "model_id": "deterministic-rerank-v2",
            },
        )
        enabled.raise_for_status()
        assert enabled.json()["enabled"] is True
        assert "rerank-v2-secret" not in enabled.text

        accepted = _submit_ingest(
            client,
            knowledge_base_id,
            key=f"ingest-{uuid.uuid4()}",
            attributes={"department_id": "lifecycle"},
        )
        accepted.raise_for_status()
        completed = _wait_operation(client, accepted.json()["operation_id"])
        assert completed["status"] == "completed", completed
        reranked = client.post(
            "/knowledge/v1/search",
            headers=_runtime_headers(),
            json={"query": "退款", "knowledge_base_ids": [knowledge_base_id], "mode": "hybrid"},
        )
        reranked.raise_for_status()
        assert reranked.json()["hits"][0]["score"] == 1000.0

        rotated = client.put(
            f"/knowledge/v1/knowledge-bases/{knowledge_base_id}/embedding-config",
            headers=_admin_headers(),
            json={
                "base_url": "http://embedding-stub:18080/v1",
                "api_key": "embedding-v2-rotated",
                "model_id": "deterministic-v2",
                "dimension": 5,
            },
        )
        rotated.raise_for_status()
        assert rotated.json()["locked"] is True

        rejected = client.put(
            f"/knowledge/v1/knowledge-bases/{knowledge_base_id}/embedding-config",
            headers=_admin_headers(),
            json={
                "base_url": "http://embedding-stub:18080/v1",
                "api_key": "embedding-v3-secret",
                "model_id": "deterministic-v3",
                "dimension": 4,
            },
        )
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "embedding_config_locked"

        disabled = client.put(
            f"/knowledge/v1/knowledge-bases/{knowledge_base_id}/rerank-config",
            headers=_admin_headers(),
            json={"enabled": False},
        )
        disabled.raise_for_status()
        assert disabled.json()["enabled"] is False
        assert disabled.json()["credential_configured"] is False
        without_rerank = client.post(
            "/knowledge/v1/search",
            headers=_runtime_headers(),
            json={"query": "退款", "knowledge_base_ids": [knowledge_base_id], "mode": "hybrid"},
        )
        without_rerank.raise_for_status()
        assert without_rerank.json()["hits"][0]["score"] != 1000.0

        config = client.get(
            f"/knowledge/v1/knowledge-bases/{knowledge_base_id}/inference-config",
            headers=_admin_headers(),
        )
        config.raise_for_status()
        assert config.json()["embedding"]["model_id"] == "deterministic-v2"
        assert config.json()["rerank"]["enabled"] is False

        _delete_knowledge_base(client, knowledge_base_id)
        assert (
            client.get(
                f"/knowledge/v1/knowledge-bases/{knowledge_base_id}/inference-config",
                headers=_admin_headers(),
            ).status_code
            == 404
        )
        knowledge_base_id = None
    if knowledge_base_id is not None:
        with httpx.Client(base_url=_base_url(), timeout=30, trust_env=False) as cleanup_client:
            _delete_knowledge_base(cleanup_client, knowledge_base_id)


def test_failed_create_can_retry_same_idempotency_key_without_orphaned_profile() -> None:
    tenant_id = f"create-failure-{uuid.uuid4().hex[:10]}"
    idempotency_key = f"create-{uuid.uuid4()}"
    with httpx.Client(base_url=_base_url(), timeout=30, trust_env=False) as client:
        failed = client.post(
            "/knowledge/v1/knowledge-bases",
            headers=_runtime_headers(**{"Idempotency-Key": idempotency_key}),
            json={
                "tenant_id": tenant_id,
                "embedding": {
                    "base_url": "http://embedding-stub:18080/v1",
                    "api_key": "wrong-key",
                    "model_id": "keyed-embedding-a",
                    "dimension": 3,
                },
            },
        )
        assert failed.status_code == 502
        assert failed.json()["error"]["code"] == "inference_rejected"

        created = _create_knowledge_base(
            client,
            tenant_id,
            key=idempotency_key,
            embedding_key="embedding-key-a",
            embedding_model="keyed-embedding-a",
            dimension=3,
        )
        _delete_knowledge_base(client, str(created["knowledge_base_id"]))


@pytest.mark.recovery
def test_failed_operation_can_retry_once_without_reuploading_source() -> None:
    """产品只提交 Retry；服务复用原 File，并保留旧 Operation 的不可变关系。"""

    if os.environ.get("KNOWLEDGE_FAILURE_E2E") != "1":
        pytest.skip("未设置 KNOWLEDGE_FAILURE_E2E=1")
    tenant_id = f"retry-{uuid.uuid4().hex[:10]}"
    knowledge_base_id: str | None = None
    _set_embedding_stub_failure(False)
    try:
        with httpx.Client(base_url=_base_url(), timeout=120, trust_env=False) as client:
            knowledge_base_id = str(_create_knowledge_base(client, tenant_id)["knowledge_base_id"])

            _set_embedding_stub_failure(True)
            accepted = _submit_ingest(
                client,
                knowledge_base_id,
                key=f"ingest-{uuid.uuid4()}",
                attributes={"department_id": "retry"},
            )
            accepted.raise_for_status()
            failed = _wait_operation(client, accepted.json()["operation_id"])
            assert failed["status"] == "failed", failed
            assert failed["retryable"] is True

            _set_embedding_stub_failure(False)
            retried = client.post(
                f"/knowledge/v1/operations/{accepted.json()['operation_id']}/retry",
                headers=_runtime_headers(),
            )
            retried.raise_for_status()
            assert retried.status_code == 202
            assert retried.json()["file_id"] == accepted.json()["file_id"]
            completed = _wait_operation(client, retried.json()["operation_id"])
            assert completed["status"] == "completed", completed
            assert completed["retried_from_operation_id"] == accepted.json()["operation_id"]

            replayed_retry = client.post(
                f"/knowledge/v1/operations/{accepted.json()['operation_id']}/retry",
                headers=_runtime_headers(),
            )
            replayed_retry.raise_for_status()
            assert replayed_retry.status_code == 200
            assert replayed_retry.json()["operation_id"] == retried.json()["operation_id"]

            _delete_knowledge_base(client, knowledge_base_id)
            knowledge_base_id = None
    finally:
        _set_embedding_stub_failure(False)
        if knowledge_base_id is not None:
            with httpx.Client(base_url=_base_url(), timeout=30, trust_env=False) as cleanup_client:
                _delete_knowledge_base(cleanup_client, knowledge_base_id)
