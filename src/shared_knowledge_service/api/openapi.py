"""统一 Knowledge API 的产品 OpenAPI 与 Scalar 文档页面。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter
from fastapi.openapi.utils import get_openapi
from scalar_fastapi import AgentScalarConfig, get_scalar_api_reference
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import BaseRoute

API_TITLE = "Stella 与 Cherry Studio 企业版统一知识库 API"
API_VERSION = "0.1.0"
OPENAPI_PATH = "/knowledge-openapi.json"
SCALAR_PATH = "/api-docs"
SCALAR_JS_URL = "https://cdn.jsdelivr.net/npm/@scalar/api-reference@1.66.1"
SERVICE_TOKEN_SCHEME = "ServiceToken"

_ADMIN_OPERATIONS = {
    ("/knowledge/v1/knowledge-bases/{knowledge_base_id}/inference-config", "get"),
    ("/knowledge/v1/knowledge-bases/{knowledge_base_id}/embedding-config", "put"),
    ("/knowledge/v1/knowledge-bases/{knowledge_base_id}/rerank-config", "put"),
}

_REQUEST_EXAMPLES: dict[tuple[str, str], dict[str, dict[str, Any]]] = {
    ("/knowledge/v1/knowledge-bases", "post"): {
        "enterprise": {
            "summary": "企业版显式业务知识库",
            "value": {
                "tenant_id": "tenant-company-a",
                "embedding": {
                    "base_url": "https://newapi.example.com/v1",
                    "api_key": "<embedding-api-key>",
                    "model_id": "embedding/model-id",
                    "dimension": 1024,
                },
                "rerank": {
                    "base_url": "https://newapi.example.com/v1",
                    "api_key": "<rerank-api-key>",
                    "model_id": "rerank/model-id",
                },
            },
        },
        "stella": {
            "summary": "Stella 初始化隐藏知识库",
            "value": {
                "tenant_id": "stella-deployment",
                "embedding": {
                    "base_url": "https://models.example.com/v1",
                    "api_key": "<embedding-api-key>",
                    "model_id": "embedding/model-id",
                },
            },
        },
    },
    ("/knowledge/v1/knowledge-bases/{knowledge_base_id}/embedding-config", "put"): {
        "replace-empty-knowledge-base-config": {
            "summary": "空知识库更新 Embedding 配置",
            "value": {
                "base_url": "https://newapi.example.com/v1",
                "api_key": "<embedding-api-key>",
                "model_id": "embedding/model-id",
                "dimension": 1024,
            },
        }
    },
    ("/knowledge/v1/knowledge-bases/{knowledge_base_id}/rerank-config", "put"): {
        "enable": {
            "summary": "启用或更换 Rerank",
            "value": {
                "enabled": True,
                "base_url": "https://newapi.example.com/v1",
                "api_key": "<rerank-api-key>",
                "model_id": "rerank/model-id",
            },
        },
        "disable": {
            "summary": "关闭 Rerank",
            "value": {"enabled": False},
        },
    },
    ("/knowledge/v1/knowledge-bases/{knowledge_base_id}/files/query", "post"): {
        "enterprise": {
            "summary": "企业版按业务属性查看文件",
            "value": {
                "filters": {"type": "eq", "key": "department_id", "value": "product-a"},
                "statuses": ["completed", "failed"],
                "cursor": None,
                "limit": 20,
            },
        },
        "stella": {
            "summary": "Stella 查看隐藏知识库文件",
            "value": {
                "filters": {"type": "eq", "key": "scope", "value": "user"},
                "statuses": ["processing", "completed", "failed"],
                "cursor": None,
                "limit": 20,
            },
        },
    },
    ("/knowledge/v1/search", "post"): {
        "enterprise-mounted-knowledge-bases": {
            "summary": "企业版跨挂载知识库等权融合",
            "value": {
                "query": "产品 A 的退款流程是什么？",
                "knowledge_base_ids": ["kb-company", "kb-product-a"],
                "filters": None,
                "mode": "hybrid",
                "limit": 10,
            },
        },
        "stella-four-quadrants": {
            "summary": "Stella 四象限属性过滤",
            "value": {
                "query": "我的项目约定是什么？",
                "knowledge_base_ids": ["kb-stella"],
                "filters": {
                    "type": "or",
                    "filters": [
                        {"type": "eq", "key": "scope", "value": "system"},
                        {
                            "type": "and",
                            "filters": [
                                {"type": "eq", "key": "scope", "value": "user"},
                                {"type": "eq", "key": "user_id", "value": "user-123"},
                            ],
                        },
                    ],
                },
                "mode": "hybrid",
                "limit": 10,
            },
        },
    },
}

_RESPONSE_EXAMPLES: dict[tuple[str, str, str], dict[str, Any]] = {
    ("/knowledge/v1/ingest", "post", "202"): {
        "operation_id": "op_01HZXEXAMPLE",
        "file_id": "file_01HZXEXAMPLE",
        "knowledge_base_id": "kb-product-a",
        "status": "processing",
    },
    ("/knowledge/v1/operations/{operation_id}", "get", "200"): {
        "operation_id": "op_01HZXEXAMPLE",
        "knowledge_base_id": "kb-product-a",
        "file_id": "file_01HZXEXAMPLE",
        "status": "completed",
        "created_at": "2026-08-27T10:00:00Z",
        "last_error": None,
        "retryable": False,
        "retried_from_operation_id": None,
        "retried_by_operation_id": None,
    },
    ("/knowledge/v1/search", "post", "200"): {
        "hits": [
            {
                "knowledge_base_id": "kb-product-a",
                "file_id": "file_01HZXEXAMPLE",
                "filename": "refund-policy.pdf",
                "chunk_id": "chunk_01HZXEXAMPLE",
                "content": "申请退款时需要提供订单编号。",
                "locator": {"page": 3, "headings": ["退款流程"]},
                "score": 0.0325,
                "attributes": {"department_id": "product-a"},
            }
        ]
    },
}


def _set_request_examples(operation: dict[str, Any], examples: dict[str, dict[str, Any]]) -> None:
    """把 JSON 请求示例挂到 operation，供 Scalar 直接选择。"""

    request_body = operation.get("requestBody")
    if not isinstance(request_body, dict):
        return
    content = request_body.get("content")
    if not isinstance(content, dict):
        return
    media_type = content.get("application/json")
    if isinstance(media_type, dict):
        media_type["examples"] = examples


def _set_response_example(operation: dict[str, Any], status: str, example: dict[str, Any]) -> None:
    """添加成功响应示例，不改动 FastAPI 自动生成的响应 Schema。"""

    response = operation.get("responses", {}).get(status)
    if not isinstance(response, dict):
        return
    media_type = response.get("content", {}).get("application/json")
    if isinstance(media_type, dict):
        media_type["example"] = example


def _set_parameter_examples(path: str, operation: dict[str, Any]) -> None:
    """为常用路径参数提供可替换的技术 ID 示例。"""

    examples = {
        "knowledge_base_id": "kb-product-a",
        "operation_id": "op_01HZXEXAMPLE",
        "file_id": "file_01HZXEXAMPLE",
    }
    for parameter in operation.get("parameters", []):
        if not isinstance(parameter, dict):
            continue
        if parameter.get("in") == "header" and parameter.get("name") == "Idempotency-Key":
            parameter["example"] = "product-request-0001"
            continue
        if parameter.get("in") != "path":
            continue
        name = parameter.get("name")
        if name in examples and "{" + str(name) + "}" in path:
            parameter["example"] = examples[name]


def _describe_auth(path: str, method: str, operation: dict[str, Any]) -> None:
    """声明统一 Bearer 协议，并明确每个接口需要的 Token 等级。"""

    admin_only = (path, method) in _ADMIN_OPERATIONS
    required_token = "Admin Token" if admin_only else "Runtime Token 或 Admin Token"
    original = operation.get("description", "")
    auth_description = f"**鉴权：{required_token}。**"
    operation["description"] = f"{auth_description}\n\n{original}" if original else auth_description
    operation["security"] = [{SERVICE_TOKEN_SCHEME: []}]
    operation["x-required-token"] = "admin" if admin_only else "runtime-or-admin"


def _describe_ingest_form(schema: dict[str, Any], operation: dict[str, Any]) -> None:
    """让 Scalar 的 multipart 表单显示可理解的文件与属性示例。"""

    media_type = operation.get("requestBody", {}).get("content", {}).get("multipart/form-data")
    if not isinstance(media_type, dict):
        return
    schema_ref = media_type.get("schema", {}).get("$ref")
    if not isinstance(schema_ref, str):
        return
    component_name = schema_ref.rsplit("/", maxsplit=1)[-1]
    component = schema.get("components", {}).get("schemas", {}).get(component_name)
    if not isinstance(component, dict):
        return
    properties = component.get("properties", {})
    if isinstance(properties.get("file"), dict):
        # FastAPI 3.1 使用 contentMediaType；补 format 兼容 Scalar 等常见文件控件识别方式。
        properties["file"]["format"] = "binary"
    if isinstance(properties.get("knowledge_base_id"), dict):
        properties["knowledge_base_id"]["example"] = "kb-product-a"
    if isinstance(properties.get("attributes"), dict):
        properties["attributes"]["example"] = '{"department_id":"product-a"}'


def build_product_openapi(routes: Sequence[BaseRoute]) -> dict[str, Any]:
    """只从统一 Knowledge API 路由生成可交付给产品调用方的契约。"""

    schema = get_openapi(
        title=API_TITLE,
        version=API_VERSION,
        summary="Stella 与 Cherry Studio 企业版共享的知识库服务契约",
        description=(
            "本契约只包含稳定的 `/knowledge/v1/*` 产品接口，不包含 OGX 原生内部接口。"
            "普通业务接口使用 Runtime Token；Admin Token 是其超集，并用于模型配置接口。"
        ),
        routes=list(routes),
        tags=[
            {
                "name": "Knowledge",
                "description": "知识库对象、异步文件导入、文件状态管理与检索。",
            }
        ],
    )
    schema["paths"] = {
        path: value
        for path, value in schema.get("paths", {}).items()
        if path.startswith("/knowledge/v1/") and "/internal/" not in path
    }
    schema["servers"] = [{"url": "/", "description": "当前部署"}]
    components = schema.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    security_schemes[SERVICE_TOKEN_SCHEME] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "opaque service token",
        "description": (
            "产品运行时填写 Runtime Token；模型配置接口填写 Admin Token。"
            "Admin Token 也可以调用普通业务接口。Token 由每套部署生成，不写入本 OpenAPI 文档。"
        ),
    }

    for path, path_item in schema["paths"].items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "delete", "patch"} or not isinstance(operation, dict):
                continue
            _describe_auth(path, method, operation)
            _set_parameter_examples(path, operation)
            request_examples = _REQUEST_EXAMPLES.get((path, method))
            if request_examples:
                _set_request_examples(operation, request_examples)
            for response_path, response_method, status in _RESPONSE_EXAMPLES:
                if (response_path, response_method) == (path, method):
                    _set_response_example(operation, status, _RESPONSE_EXAMPLES[(path, method, status)])
            if (path, method) == ("/knowledge/v1/ingest", "post"):
                _describe_ingest_form(schema, operation)

    return schema


def register_documentation_routes(router: APIRouter, schema: dict[str, Any]) -> None:
    """在现有 FastAPI Router 上注册 OpenAPI 下载和 Scalar 测试页面。"""

    @router.get(OPENAPI_PATH, include_in_schema=False)
    async def product_openapi() -> JSONResponse:
        """返回不包含 OGX 原生接口的产品 OpenAPI。"""

        return JSONResponse(schema)

    @router.get(SCALAR_PATH, include_in_schema=False)
    async def scalar_api_docs() -> HTMLResponse:
        """渲染 Scalar 交互式接口文档；页面中不预置任何 Token。"""

        return get_scalar_api_reference(
            openapi_url=OPENAPI_PATH,
            title=f"{API_TITLE} · 接口调试",
            # 固定前端版本，避免 CDN 的 latest 在未改代码时改变页面行为。
            scalar_js_url=SCALAR_JS_URL,
            persist_auth=False,
            telemetry=False,
            # 调试页只用于查契约和发请求，不启用会访问外部模型的 Scalar Agent。
            agent=AgentScalarConfig(disabled=True),
            show_developer_tools="never",
        )
