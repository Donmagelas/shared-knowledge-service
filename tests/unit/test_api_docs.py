"""产品 OpenAPI 与 Scalar 页面契约测试。"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shared_knowledge_service.api.errors import ApiSecurity
from shared_knowledge_service.api.openapi import API_TITLE, SCALAR_JS_URL, SERVICE_TOKEN_SCHEME
from shared_knowledge_service.api.routes import create_router

_PRODUCT_PATHS = {
    "/knowledge/v1/knowledge-bases",
    "/knowledge/v1/knowledge-bases/{knowledge_base_id}",
    "/knowledge/v1/knowledge-bases/{knowledge_base_id}/inference-config",
    "/knowledge/v1/knowledge-bases/{knowledge_base_id}/embedding-config",
    "/knowledge/v1/knowledge-bases/{knowledge_base_id}/rerank-config",
    "/knowledge/v1/ingest",
    "/knowledge/v1/ingest/batch",
    "/knowledge/v1/operations/{operation_id}",
    "/knowledge/v1/operations/{operation_id}/retry",
    "/knowledge/v1/knowledge-bases/{knowledge_base_id}/files/query",
    "/knowledge/v1/knowledge-bases/{knowledge_base_id}/files/{file_id}",
    "/knowledge/v1/knowledge-bases/{knowledge_base_id}/files/{file_id}/download",
    "/knowledge/v1/search",
}


def _client() -> TestClient:
    """创建不依赖真实存储的文档路由测试客户端。"""

    impl = SimpleNamespace(security=ApiSecurity("runtime-token-1234", "admin-token-123456"))
    app = FastAPI()
    app.include_router(create_router(impl))  # type: ignore[arg-type]
    return TestClient(app)


def test_product_openapi_contains_only_stable_knowledge_routes() -> None:
    """产品契约不能泄露同进程的 OGX 原生接口和内部鉴权回调。"""

    response = _client().get("/knowledge-openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert schema["openapi"] == "3.1.0"
    assert schema["info"]["title"] == API_TITLE
    assert schema["servers"] == [{"url": "/", "description": "当前部署"}]
    assert set(schema["paths"]) == _PRODUCT_PATHS
    assert not any(path.startswith("/v1/") or "/internal/" in path for path in schema["paths"])


def test_product_openapi_describes_service_token_levels() -> None:
    """Scalar 应显示一个 Bearer 输入框，并在接口上区分 Runtime/Admin 等级。"""

    schema = _client().get("/knowledge-openapi.json").json()
    security_scheme = schema["components"]["securitySchemes"][SERVICE_TOKEN_SCHEME]
    assert security_scheme["type"] == "http"
    assert security_scheme["scheme"] == "bearer"
    assert "Runtime Token" in security_scheme["description"]
    assert "Admin Token" in security_scheme["description"]

    runtime_operation = schema["paths"]["/knowledge/v1/search"]["post"]
    admin_operation = schema["paths"]["/knowledge/v1/knowledge-bases/{knowledge_base_id}/embedding-config"]["put"]
    assert runtime_operation["security"] == [{SERVICE_TOKEN_SCHEME: []}]
    assert runtime_operation["x-required-token"] == "runtime-or-admin"
    assert "Runtime Token 或 Admin Token" in runtime_operation["description"]
    assert admin_operation["security"] == [{SERVICE_TOKEN_SCHEME: []}]
    assert admin_operation["x-required-token"] == "admin"
    assert "Admin Token" in admin_operation["description"]


def test_product_openapi_contains_product_examples_and_file_picker_schema() -> None:
    """两端示例、异步任务示例和 multipart 文件控件都应进入契约。"""

    schema = _client().get("/knowledge-openapi.json").json()

    create_operation = schema["paths"]["/knowledge/v1/knowledge-bases"]["post"]
    create_examples = create_operation["requestBody"]["content"]["application/json"]["examples"]
    assert set(create_examples) == {"enterprise", "stella"}
    idempotency_header = next(
        parameter
        for parameter in create_operation["parameters"]
        if parameter["in"] == "header" and parameter["name"] == "Idempotency-Key"
    )
    assert idempotency_header["example"] == "product-request-0001"

    file_examples = schema["paths"]["/knowledge/v1/knowledge-bases/{knowledge_base_id}/files/query"]["post"][
        "requestBody"
    ]["content"]["application/json"]["examples"]
    search_examples = schema["paths"]["/knowledge/v1/search"]["post"]["requestBody"]["content"]["application/json"][
        "examples"
    ]
    assert set(file_examples) == {"enterprise", "stella"}
    assert set(search_examples) == {"enterprise-mounted-knowledge-bases", "stella-four-quadrants"}

    ingest_operation = schema["paths"]["/knowledge/v1/ingest"]["post"]
    multipart_schema_ref = ingest_operation["requestBody"]["content"]["multipart/form-data"]["schema"]["$ref"]
    multipart_schema = schema["components"]["schemas"][multipart_schema_ref.rsplit("/", maxsplit=1)[-1]]
    assert multipart_schema["properties"]["file"]["format"] == "binary"
    assert multipart_schema["properties"]["knowledge_base_id"]["example"] == "kb-product-a"
    assert multipart_schema["properties"]["attributes"]["example"] == '{"department_id":"product-a"}'
    assert ingest_operation["responses"]["202"]["content"]["application/json"]["example"]["status"] == "processing"

    batch_operation = schema["paths"]["/knowledge/v1/ingest/batch"]["post"]
    batch_schema_ref = batch_operation["requestBody"]["content"]["multipart/form-data"]["schema"]["$ref"]
    batch_schema = schema["components"]["schemas"][batch_schema_ref.rsplit("/", maxsplit=1)[-1]]
    assert batch_schema["properties"]["files"]["items"]["format"] == "binary"
    assert batch_schema["properties"]["knowledge_base_id"]["example"] == "kb-product-a"
    assert batch_schema["properties"]["attributes"]["example"] == '{"department_id":"product-a"}'
    assert len(batch_operation["responses"]["202"]["content"]["application/json"]["example"]["items"]) == 2

    download_operation = schema["paths"]["/knowledge/v1/knowledge-bases/{knowledge_base_id}/files/{file_id}/download"][
        "get"
    ]
    download_content = download_operation["responses"]["200"]["content"]
    assert download_content["application/octet-stream"]["schema"] == {"type": "string", "format": "binary"}


def test_scalar_page_uses_product_openapi_without_embedding_credentials() -> None:
    """Scalar 页面只引用产品契约，不能预置测试 Token。"""

    response = _client().get("/api-docs")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "/knowledge-openapi.json" in response.text
    assert SCALAR_JS_URL in response.text
    assert '"agent": {"disabled": true}' in response.text
    assert "runtime-token-1234" not in response.text
    assert "admin-token-123456" not in response.text
