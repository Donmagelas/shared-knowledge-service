"""通过 OGX 原生 API 验证完整文件导入与检索链路。"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

pytestmark = [pytest.mark.e2e]


def _compose_command() -> list[str]:
    """测试服务同时由生产 Compose 与 E2E 覆盖文件定义。"""

    return ["docker", "compose", "-f", "compose.yaml", "-f", "compose.e2e.yaml"]


def _fixture_bytes() -> bytes:
    fixture_path = Path(__file__).parents[1] / "fixtures" / "knowledge.md"
    return fixture_path.read_bytes()


def _upload_bytes(client: httpx.Client, *, filename: str, content: bytes, content_type: str) -> str:
    response = client.post(
        "/v1/files",
        files={"file": (filename, content, content_type)},
        data={"purpose": "assistants"},
    )
    response.raise_for_status()
    return str(response.json()["id"])


def _upload_fixture(client: httpx.Client) -> str:
    return _upload_bytes(
        client,
        filename="knowledge.md",
        content=_fixture_bytes(),
        content_type="text/markdown",
    )


def _minimal_text_pdf() -> bytes:
    """生成无需额外 PDF 依赖的单页数字 PDF，用于验证真实 Docling PDF 路径。"""

    stream = b"BT /F1 12 Tf 72 720 Td (Refund requires order number and payment receipt.) Tj ET"
    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\nendobj\n",
        b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
        b"5 0 obj\n<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream\nendobj\n",
    ]
    document = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for object_ in objects:
        offsets.append(len(document))
        document.extend(object_)
    xref_offset = len(document)
    document.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    document.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        document.extend(f"{offset:010d} 00000 n \n".encode())
    document.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode())
    return bytes(document)


def _create_vector_store(client: httpx.Client, *, tenant_id: str = "e2e-default") -> str:
    response = client.post(
        "/v1/vector_stores",
        json={
            "name": f"e2e-{uuid.uuid4().hex[:8]}",
            "metadata": {"test": True, "tenant_id": tenant_id},
        },
    )
    response.raise_for_status()
    return str(response.json()["id"])


def _attach_file(
    client: httpx.Client,
    vector_store_id: str,
    file_id: str,
    *,
    department_id: str,
) -> None:
    response = client.post(
        f"/v1/vector_stores/{vector_store_id}/files",
        json={"file_id": file_id, "attributes": {"department_id": department_id}},
    )
    response.raise_for_status()
    body = response.json()
    assert body["status"] == "completed", body


def _submit_knowledge_ingest(
    client: httpx.Client,
    vector_store_id: str,
    *,
    filename: str,
    department_id: str,
    content: bytes | None = None,
    content_type: str = "text/markdown",
) -> dict[str, object]:
    """提交单文件异步导入，并返回已经持久化的任务标识。"""

    response = client.post(
        "/knowledge/v1/ingest",
        files={"file": (filename, content if content is not None else _fixture_bytes(), content_type)},
        data={
            "knowledge_base_id": vector_store_id,
            "attributes": json.dumps({"department_id": department_id}),
        },
    )
    response.raise_for_status()
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "processing", body
    assert body["operation_id"]
    assert body["file_id"]
    return body


def _wait_knowledge_ingest(
    client: httpx.Client,
    vector_store_id: str,
    operation_id: str,
    *,
    timeout: float = 180,
) -> dict[str, object]:
    """轮询统一状态接口，返回 completed、failed 或 cancelled 终态。"""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(
            f"/knowledge/v1/knowledge-bases/{vector_store_id}/operations/{operation_id}",
        )
        response.raise_for_status()
        body = response.json()
        if body["status"] in {"completed", "failed", "cancelled"}:
            return body
        time.sleep(0.1)
    raise TimeoutError(f"异步导入任务 {operation_id} 在 {timeout} 秒内未完成")


def _knowledge_ingest(
    client: httpx.Client,
    vector_store_id: str,
    *,
    filename: str,
    department_id: str,
) -> str:
    """提交异步导入并等待成功，供不关心中间状态的 E2E 场景复用。"""

    submitted = _submit_knowledge_ingest(
        client,
        vector_store_id,
        filename=filename,
        department_id=department_id,
    )
    completed = _wait_knowledge_ingest(client, vector_store_id, str(submitted["operation_id"]))
    assert completed["status"] == "completed", completed
    return str(submitted["file_id"])


def _search(
    client: httpx.Client,
    vector_store_id: str,
    *,
    department_id: str,
    search_mode: str = "hybrid",
    query: str = "退款申请需要什么材料",
) -> dict[str, object]:
    response = client.post(
        f"/v1/vector_stores/{vector_store_id}/search",
        json={
            "query": query,
            "filters": {"type": "eq", "key": "department_id", "value": department_id},
            "max_num_results": 5,
            "search_mode": search_mode,
        },
    )
    response.raise_for_status()
    return response.json()


def _restart_compose_service(service: str) -> None:
    """重启指定服务，并等待 Compose 健康检查重新通过。"""

    repository_root = Path(__file__).parents[2]
    subprocess.run(
        [*_compose_command(), "restart", service],
        cwd=repository_root,
        check=True,
        timeout=60,
    )
    container_id = subprocess.check_output(
        [*_compose_command(), "ps", "-q", service],
        cwd=repository_root,
        text=True,
        timeout=10,
    ).strip()
    if not container_id:
        raise RuntimeError(f"Compose 服务不存在：{service}")

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        health = subprocess.check_output(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", container_id],
            text=True,
            timeout=10,
        ).strip()
        if health == "healthy":
            return
        time.sleep(1)
    raise TimeoutError(f"Compose 服务未恢复 healthy：{service}")


def _set_embedding_stub_failure(enabled: bool) -> None:
    """通过容器内临时文件切换测试 Stub 的确定性故障。"""

    repository_root = Path(__file__).parents[2]
    stub_service = os.environ.get("OGX_EMBEDDING_STUB_SERVICE", "embedding-stub")
    container_id = subprocess.check_output(
        [*_compose_command(), "ps", "-q", stub_service],
        cwd=repository_root,
        text=True,
        timeout=10,
    ).strip()
    if not container_id:
        raise RuntimeError(f"Embedding Stub 服务不存在：{stub_service}")
    command = ["touch", "/tmp/embedding_stub.fail"] if enabled else ["rm", "-f", "/tmp/embedding_stub.fail"]
    subprocess.run(["docker", "exec", container_id, *command], check=True, timeout=10)


def _kill_docling_workers() -> int:
    """终止当前 OGX 文件处理子进程，保留父进程以验证 WorkerPool 自动拉起。"""

    repository_root = Path(__file__).parents[2]
    container_id = subprocess.check_output(
        [*_compose_command(), "ps", "-q", "knowledge-ogx"],
        cwd=repository_root,
        text=True,
        timeout=10,
    ).strip()
    if not container_id:
        raise RuntimeError("knowledge-ogx 容器不存在")

    kill_script = (
        "import os, signal\n"
        "from pathlib import Path\n"
        "needle = b'multiprocessing.' + b'spawn'\n"
        "pids = []\n"
        "for entry in Path('/proc').glob('[0-9]*/cmdline'):\n"
        "    try:\n"
        "        if needle in entry.read_bytes():\n"
        "            pids.append(int(entry.parent.name))\n"
        "    except OSError:\n"
        "        pass\n"
        "for pid in pids:\n"
        "    os.kill(pid, signal.SIGKILL)\n"
        "print(len(pids))\n"
    )
    killed = subprocess.check_output(
        ["docker", "exec", container_id, "/app/.venv/bin/python", "-c", kill_script],
        text=True,
        timeout=10,
    ).strip()
    return int(killed)


def test_native_file_ingestion_and_hybrid_search() -> None:
    base_url = os.environ.get("OGX_E2E_URL")
    if not base_url:
        pytest.skip("未设置 OGX_E2E_URL")

    client = httpx.Client(base_url=base_url, timeout=180, trust_env=False)
    file_id: str | None = None
    vector_store_id: str | None = None
    try:
        file_id = _upload_fixture(client)
        vector_store_id = _create_vector_store(client)
        _attach_file(client, vector_store_id, file_id, department_id="product-a")

        response = _search(client, vector_store_id, department_id="product-a")
        assert response["data"]
        assert any("订单编号" in item["content"][0]["text"] for item in response["data"])
        assert all(item["attributes"]["department_id"] == "product-a" for item in response["data"])
    finally:
        if vector_store_id is not None:
            delete_vector_store = client.delete(f"/v1/vector_stores/{vector_store_id}")
            assert delete_vector_store.status_code in {200, 404}
        if file_id is not None:
            delete_file = client.delete(f"/v1/files/{file_id}")
            assert delete_file.status_code in {200, 404}
        client.close()


def test_minimal_distribution_only_registers_required_api_families() -> None:
    base_url = os.environ.get("OGX_E2E_URL")
    if not base_url:
        pytest.skip("未设置 OGX_E2E_URL")

    schema = httpx.get(f"{base_url}/openapi.json", timeout=30, trust_env=False).json()
    paths = set(schema["paths"])
    required = {
        "/v1/files",
        "/v1/vector_stores",
        "/v1/vector_stores/{vector_store_id}/search",
        "/v1alpha/file-processors/jobs",
        "/knowledge/v1/ingest",
        "/knowledge/v1/knowledge-bases/{knowledge_base_id}/operations/{operation_id}",
        "/knowledge/v1/search",
    }
    assert required <= paths
    for forbidden_prefix in ("/v1/agents", "/v1/responses", "/v1/messages", "/v1/tools", "/v1/evals"):
        assert not any(path.startswith(forbidden_prefix) for path in paths)


def test_unified_api_ingests_and_searches_multiple_knowledge_bases_once() -> None:
    """验证企业版多知识库和 Stella 单隐藏库共用同一稳定检索契约。"""

    base_url = os.environ.get("OGX_E2E_URL")
    if not base_url:
        pytest.skip("未设置 OGX_E2E_URL")

    client = httpx.Client(base_url=base_url, timeout=180, trust_env=False)
    vector_store_ids: list[str] = []
    file_ids: list[str] = []
    try:
        company_store = _create_vector_store(client)
        product_store = _create_vector_store(client)
        vector_store_ids.extend([company_store, product_store])
        company_file = _knowledge_ingest(
            client,
            company_store,
            filename="company.md",
            department_id="company",
        )
        product_file = _knowledge_ingest(
            client,
            product_store,
            filename="product-a.md",
            department_id="product-a",
        )
        file_ids.extend([company_file, product_file])

        response = client.post(
            "/knowledge/v1/search",
            json={
                "query": "退款申请需要什么材料",
                "knowledge_base_ids": [company_store, product_store],
                "mode": "hybrid",
                "limit": 10,
            },
        )
        response.raise_for_status()
        hits = response.json()["hits"]
        assert {hit["file_id"] for hit in hits} == {company_file, product_file}
        assert all(set(hit) == {"file_id", "chunk_id", "content", "locator", "score", "attributes"} for hit in hits)
        if os.environ.get("E2E_EXPECT_RERANK", "1") == "1":
            # 测试 Reranker 返回 1000 起始的特殊分数，避免把 Qdrant RRF 分数误判为已完成远程重排。
            assert hits[0]["score"] == 1000.0
        else:
            assert hits[0]["score"] != 1000.0

        # Stub 只是文本哈希，不具备语义相似性；用已命中原文验证 Dense 协议，避免把随机余弦当效果评测。
        exact_chunk_text = hits[0]["content"]
        for mode, mode_query in (("dense", exact_chunk_text), ("bm25", "退款申请需要什么材料")):
            mode_response = client.post(
                "/knowledge/v1/search",
                json={
                    "query": mode_query,
                    "knowledge_base_ids": [company_store, product_store],
                    "mode": mode,
                    "limit": 10,
                },
            )
            mode_response.raise_for_status()
            assert {hit["file_id"] for hit in mode_response.json()["hits"]} == {company_file, product_file}

        filtered = client.post(
            "/knowledge/v1/search",
            json={
                "query": "退款申请需要什么材料",
                "knowledge_base_ids": [company_store, product_store],
                "filters": {"type": "eq", "key": "department_id", "value": "product-a"},
            },
        )
        filtered.raise_for_status()
        filtered_hits = filtered.json()["hits"]
        assert filtered_hits
        assert {hit["file_id"] for hit in filtered_hits} == {product_file}
        assert all(hit["attributes"]["department_id"] == "product-a" for hit in filtered_hits)
    finally:
        for vector_store_id in vector_store_ids:
            delete_vector_store = client.delete(f"/v1/vector_stores/{vector_store_id}")
            assert delete_vector_store.status_code in {200, 404}
        for file_id in file_ids:
            delete_file = client.delete(f"/v1/files/{file_id}")
            assert delete_file.status_code in {200, 404}
        client.close()


def test_multiple_async_ingests_complete_without_losing_files() -> None:
    """同时提交多个单文件 Batch，验证小并发下状态和索引都不会丢失。"""

    base_url = os.environ.get("OGX_E2E_URL")
    if not base_url:
        pytest.skip("未设置 OGX_E2E_URL")

    client = httpx.Client(base_url=base_url, timeout=180, trust_env=False)
    vector_store_id: str | None = None
    submitted: list[dict[str, object]] = []
    try:
        vector_store_id = _create_vector_store(client)
        for index in range(4):
            submitted.append(
                _submit_knowledge_ingest(
                    client,
                    vector_store_id,
                    filename=f"async-{index}.md",
                    department_id=f"async-{index}",
                )
            )

        assert len({str(item["operation_id"]) for item in submitted}) == len(submitted)
        assert len({str(item["file_id"]) for item in submitted}) == len(submitted)
        for item in submitted:
            completed = _wait_knowledge_ingest(client, vector_store_id, str(item["operation_id"]))
            assert completed["status"] == "completed", completed

        listed = client.get(f"/v1/vector_stores/{vector_store_id}/files", params={"limit": 100})
        listed.raise_for_status()
        assert {item["file_id"] for item in submitted} <= {item["id"] for item in listed.json()["data"]}
    finally:
        if vector_store_id is not None:
            delete_vector_store = client.delete(f"/v1/vector_stores/{vector_store_id}")
            assert delete_vector_store.status_code in {200, 404}
        for item in submitted:
            delete_file = client.delete(f"/v1/files/{item['file_id']}")
            assert delete_file.status_code in {200, 404}
        client.close()


def test_unified_api_allows_same_tenant_and_rejects_cross_tenant_search() -> None:
    """验证 Collection 是租户边界，而 VectorStore 只是租户内逻辑范围。"""

    base_url = os.environ.get("OGX_E2E_URL")
    if not base_url:
        pytest.skip("未设置 OGX_E2E_URL")

    client = httpx.Client(base_url=base_url, timeout=180, trust_env=False)
    vector_store_ids: list[str] = []
    try:
        tenant_a_first = _create_vector_store(client, tenant_id="e2e-tenant-a")
        tenant_a_second = _create_vector_store(client, tenant_id="e2e-tenant-a")
        tenant_b = _create_vector_store(client, tenant_id="e2e-tenant-b")
        vector_store_ids.extend([tenant_a_first, tenant_a_second, tenant_b])

        same_tenant = client.post(
            "/knowledge/v1/search",
            json={
                "query": "空知识库路由探针",
                "knowledge_base_ids": [tenant_a_first, tenant_a_second],
                "mode": "bm25",
                "limit": 10,
            },
        )
        same_tenant.raise_for_status()
        assert same_tenant.json()["hits"] == []

        cross_tenant = client.post(
            "/knowledge/v1/search",
            json={
                "query": "跨租户路由探针",
                "knowledge_base_ids": [tenant_a_first, tenant_b],
                "mode": "bm25",
                "limit": 10,
            },
        )
        assert cross_tenant.status_code == 422
        assert "不能跨租户 Collection" in cross_tenant.text
    finally:
        for vector_store_id in vector_store_ids:
            delete_vector_store = client.delete(f"/v1/vector_stores/{vector_store_id}")
            assert delete_vector_store.status_code in {200, 404}
        client.close()


def test_digital_pdf_is_parsed_and_searchable() -> None:
    base_url = os.environ.get("OGX_E2E_URL")
    if not base_url:
        pytest.skip("未设置 OGX_E2E_URL")

    client = httpx.Client(base_url=base_url, timeout=180, trust_env=False)
    file_id: str | None = None
    vector_store_id: str | None = None
    try:
        file_id = _upload_bytes(
            client,
            filename="refund.pdf",
            content=_minimal_text_pdf(),
            content_type="application/pdf",
        )
        vector_store_id = _create_vector_store(client)
        _attach_file(client, vector_store_id, file_id, department_id="pdf-test")
        response = _search(
            client,
            vector_store_id,
            department_id="pdf-test",
            query="order number payment receipt",
        )
        assert response["data"]
        assert any("order number" in item["content"][0]["text"].lower() for item in response["data"])
    finally:
        if vector_store_id is not None:
            delete_vector_store = client.delete(f"/v1/vector_stores/{vector_store_id}")
            assert delete_vector_store.status_code in {200, 404}
        if file_id is not None:
            delete_file = client.delete(f"/v1/files/{file_id}")
            assert delete_file.status_code in {200, 404}
        client.close()


def test_one_file_can_be_attached_to_two_vector_stores_and_deleted_by_scope() -> None:
    """验证 OGX 对象生命周期不会破坏共享 Collection 中的其他逻辑知识库。"""

    base_url = os.environ.get("OGX_E2E_URL")
    if not base_url:
        pytest.skip("未设置 OGX_E2E_URL")

    client = httpx.Client(base_url=base_url, timeout=180, trust_env=False)
    file_id: str | None = None
    vector_store_ids: list[str] = []
    try:
        file_id = _upload_fixture(client)
        first_store = _create_vector_store(client)
        second_store = _create_vector_store(client)
        vector_store_ids.extend([first_store, second_store])

        _attach_file(client, first_store, file_id, department_id="product-a")
        _attach_file(client, second_store, file_id, department_id="product-b")
        assert _search(client, first_store, department_id="product-a")["data"]
        assert _search(client, second_store, department_id="product-b")["data"]

        detach = client.delete(f"/v1/vector_stores/{first_store}/files/{file_id}")
        detach.raise_for_status()
        assert not _search(client, first_store, department_id="product-a")["data"]
        assert _search(client, second_store, department_id="product-b")["data"]

        delete_first = client.delete(f"/v1/vector_stores/{first_store}")
        delete_first.raise_for_status()
        vector_store_ids.remove(first_store)
        assert _search(client, second_store, department_id="product-b")["data"]
    finally:
        for vector_store_id in vector_store_ids:
            delete_vector_store = client.delete(f"/v1/vector_stores/{vector_store_id}")
            assert delete_vector_store.status_code in {200, 404}
        if file_id is not None:
            delete_file = client.delete(f"/v1/files/{file_id}")
            assert delete_file.status_code in {200, 404}
        client.close()


@pytest.mark.recovery
def test_completed_data_survives_each_service_restart() -> None:
    """已完成数据必须在 PostgreSQL、Qdrant 与 OGX 分别重启后仍可检索。"""

    base_url = os.environ.get("OGX_E2E_URL")
    if not base_url or os.environ.get("OGX_RESTART_E2E") != "1":
        pytest.skip("需同时设置 OGX_E2E_URL 与 OGX_RESTART_E2E=1")

    client = httpx.Client(base_url=base_url, timeout=180, trust_env=False)
    file_id: str | None = None
    vector_store_id: str | None = None
    try:
        file_id = _upload_fixture(client)
        vector_store_id = _create_vector_store(client)
        _attach_file(client, vector_store_id, file_id, department_id="restart-test")

        # 只使用 BM25 做重启后的验证，避免测试用 Embedding Stub 被 OGX 重启终止后影响结论。
        before = _search(client, vector_store_id, department_id="restart-test", search_mode="keyword")
        assert before["data"]

        for service in ("qdrant", "postgres", "knowledge-ogx"):
            client.close()
            _restart_compose_service(service)
            client = httpx.Client(base_url=base_url, timeout=180, trust_env=False)
            original = client.get(f"/v1/files/{file_id}/content")
            original.raise_for_status()
            assert original.content == _fixture_bytes(), f"{service} 重启后原文件内容变化"
            after = _search(client, vector_store_id, department_id="restart-test", search_mode="keyword")
            assert after["data"], f"{service} 重启后检索结果丢失"
    finally:
        if vector_store_id is not None:
            delete_vector_store = client.delete(f"/v1/vector_stores/{vector_store_id}")
            assert delete_vector_store.status_code in {200, 404}
        if file_id is not None:
            delete_file = client.delete(f"/v1/files/{file_id}")
            assert delete_file.status_code in {200, 404}
        client.close()


@pytest.mark.recovery
def test_failed_attachment_can_be_deleted_and_retried() -> None:
    """验证 MVP 约定的显式恢复协议，而不是假定任意阶段都能自动续跑。"""

    base_url = os.environ.get("OGX_E2E_URL")
    if not base_url or os.environ.get("OGX_FAILURE_E2E") != "1":
        pytest.skip("需同时设置 OGX_E2E_URL 与 OGX_FAILURE_E2E=1")

    client = httpx.Client(base_url=base_url, timeout=180, trust_env=False)
    file_id: str | None = None
    vector_store_id: str | None = None
    try:
        _set_embedding_stub_failure(False)
        vector_store_id = _create_vector_store(client)

        _set_embedding_stub_failure(True)
        submitted = _submit_knowledge_ingest(
            client,
            vector_store_id,
            filename="knowledge.md",
            department_id="retry-test",
        )
        failed = _wait_knowledge_ingest(client, vector_store_id, str(submitted["operation_id"]))
        assert failed["status"] == "failed", failed
        assert failed["last_error"]
        file_id = str(submitted["file_id"])

        _set_embedding_stub_failure(False)
        delete_failed = client.delete(f"/v1/vector_stores/{vector_store_id}/files/{file_id}")
        delete_failed.raise_for_status()
        _attach_file(client, vector_store_id, file_id, department_id="retry-test")
        recovered = _search(client, vector_store_id, department_id="retry-test")
        assert recovered["data"]
    finally:
        _set_embedding_stub_failure(False)
        if vector_store_id is not None:
            delete_vector_store = client.delete(f"/v1/vector_stores/{vector_store_id}")
            assert delete_vector_store.status_code in {200, 404}
        if file_id is not None:
            delete_file = client.delete(f"/v1/files/{file_id}")
            assert delete_file.status_code in {200, 404}
        client.close()


@pytest.mark.recovery
def test_async_ingest_resumes_after_ogx_restart() -> None:
    """提交成功后立即重启 OGX，验证持久化 FileBatch 会恢复完整导入。"""

    base_url = os.environ.get("OGX_E2E_URL")
    if not base_url or os.environ.get("OGX_ASYNC_RESTART_E2E") != "1":
        pytest.skip("需同时设置 OGX_E2E_URL 与 OGX_ASYNC_RESTART_E2E=1")

    # 大文件用于确保重启发生时任务仍在处理，避免只验证已完成结果的读取。
    paragraphs = "".join(
        f"<h2>异步恢复规则 {index}</h2><p>退款必须提供订单编号和付款凭证。</p>" for index in range(1_000)
    )
    html = f"<!doctype html><html><body>{paragraphs}</body></html>".encode()
    client = httpx.Client(base_url=base_url, timeout=240, trust_env=False)
    vector_store_id: str | None = None
    file_id: str | None = None
    try:
        vector_store_id = _create_vector_store(client)
        submitted = _submit_knowledge_ingest(
            client,
            vector_store_id,
            filename="async-restart.html",
            department_id="async-restart",
            content=html,
            content_type="text/html",
        )
        file_id = str(submitted["file_id"])
        operation_id = str(submitted["operation_id"])

        client.close()
        _restart_compose_service("knowledge-ogx")
        client = httpx.Client(base_url=base_url, timeout=240, trust_env=False)

        completed = _wait_knowledge_ingest(
            client,
            vector_store_id,
            operation_id,
            timeout=240,
        )
        assert completed["status"] == "completed", completed
        result = _search(
            client,
            vector_store_id,
            department_id="async-restart",
            query="退款需要哪些凭证",
        )
        assert result["data"]
    finally:
        if vector_store_id is not None:
            delete_vector_store = client.delete(f"/v1/vector_stores/{vector_store_id}")
            assert delete_vector_store.status_code in {200, 404}
        if file_id is not None:
            delete_file = client.delete(f"/v1/files/{file_id}")
            assert delete_file.status_code in {200, 404}
        client.close()


@pytest.mark.recovery
def test_docling_job_is_released_after_worker_crash() -> None:
    """终止正在执行的 Worker，验证租约到期后任务会被新 Worker 重新领取。"""

    base_url = os.environ.get("OGX_E2E_URL")
    if not base_url or os.environ.get("OGX_WORKER_CRASH_E2E") != "1":
        pytest.skip("需同时设置 OGX_E2E_URL 与 OGX_WORKER_CRASH_E2E=1")

    # 足够大的受支持 HTML 让测试能稳定观察到 in_progress，而不依赖 Docling 私有注入点。
    body = "".join(f"<h2>退款规则 {index}</h2><p>申请退款必须提供订单编号和付款凭证。</p>" for index in range(5_000))
    html = f"<!doctype html><html><body>{body}</body></html>".encode()
    client = httpx.Client(base_url=base_url, timeout=180, trust_env=False)
    submit = client.post(
        "/v1alpha/file-processors/jobs",
        files={"file": ("worker-crash.html", html, "text/html")},
        data={"chunking_strategy": json.dumps({"type": "auto"})},
    )
    submit.raise_for_status()
    job_id = str(submit.json()["job_id"])

    in_progress_deadline = time.monotonic() + 20
    while time.monotonic() < in_progress_deadline:
        current = client.get(f"/v1alpha/file-processors/jobs/{job_id}")
        current.raise_for_status()
        status = current.json()["status"]
        if status == "in_progress":
            break
        if status in {"completed", "failed", "cancelled"}:
            pytest.fail(f"任务在故障注入前已结束：{status}")
        time.sleep(0.05)
    else:
        pytest.fail("20 秒内未观察到 Docling 任务进入 in_progress")

    assert _kill_docling_workers() >= 1

    terminal_deadline = time.monotonic() + 150
    while time.monotonic() < terminal_deadline:
        current = client.get(f"/v1alpha/file-processors/jobs/{job_id}")
        current.raise_for_status()
        body = current.json()
        if body["status"] == "completed":
            assert body["result"]["chunks"]
            break
        if body["status"] in {"failed", "cancelled"}:
            pytest.fail(f"Worker 崩溃后的任务未恢复：{body}")
        time.sleep(1)
    else:
        pytest.fail("Worker 崩溃后的任务在 150 秒内未结束")
    client.close()
