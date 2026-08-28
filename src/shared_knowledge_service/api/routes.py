"""统一 Knowledge API 的 FastAPI 路由。"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Header, Request, Response, UploadFile
from fastapi.routing import APIRoute
from ogx_api.common.upload_limits import (
    DEFAULT_MAX_UPLOAD_SIZE_BYTES,
    PreReadUploadFile,
    read_upload_with_size_limit,
)
from ogx_api.router_utils import PUBLIC_ROUTE_KEY
from pydantic import ValidationError

from .errors import KnowledgeError, StableKnowledgeRoute
from .models import (
    AttributeValue,
    BatchIngestResponse,
    EmbeddingConfigPutRequest,
    EmbeddingConfigResponse,
    FileDetail,
    FileQueryRequest,
    FileQueryResponse,
    IngestResponse,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseInferenceConfigResponse,
    KnowledgeBaseResponse,
    OperationResponse,
    RerankConfigPutRequest,
    RerankConfigResponse,
    RetryOperationResponse,
    SearchRequest,
    SearchResponse,
    validate_attributes,
)
from .openapi import build_product_openapi, register_documentation_routes
from .protocol import KnowledgeApi


def parse_attributes(value: str | None) -> dict[str, AttributeValue]:
    """解析并验证 multipart 表单中的 JSON attributes。"""

    if value is None or not value.strip():
        return {}
    try:
        parsed: Any = json.loads(value)
    except json.JSONDecodeError as exc:
        raise KnowledgeError(422, "invalid_attributes", f"attributes 必须是合法 JSON：{exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise KnowledgeError(422, "invalid_attributes", "attributes 必须是 JSON 对象")
    try:
        return validate_attributes(parsed)
    except ValidationError as exc:
        raise KnowledgeError(
            422,
            "invalid_attributes",
            "attributes 不符合字段、类型或数量限制",
            {"issues": exc.errors(include_url=False, include_context=False, include_input=False)},
        ) from exc


def create_router(
    impl: KnowledgeApi,
    max_upload_size_bytes: int | None = None,
) -> APIRouter:
    """创建挂载在 OGX 同一 FastAPI 进程中的产品接口。"""

    router = APIRouter(tags=["Knowledge"], route_class=StableKnowledgeRoute)
    effective_max_upload_size_bytes = (
        max_upload_size_bytes
        if max_upload_size_bytes is not None
        else int(getattr(impl, "max_upload_size_bytes", DEFAULT_MAX_UPLOAD_SIZE_BYTES))
    )

    def require_runtime(request: Request) -> None:
        impl.security.require(request)

    def require_admin(request: Request) -> None:
        impl.security.require(request, admin=True)

    @router.post(
        "/knowledge/v1/internal/auth/validate",
        include_in_schema=False,
        openapi_extra={PUBLIC_ROUTE_KEY: True},
    )
    async def validate_ogx_admin_token(request: Request) -> dict[str, object]:
        """只允许 Admin Token 访问同端口的 OGX 原生接口。

        Knowledge API 自己保留稳定错误协议；该内部回调只供 OGX 的
        ``custom`` Authentication Provider 调用，避免原生 File/VectorStore
        接口成为绕过统一接口的未鉴权入口。
        """

        try:
            payload = await request.json()
        except ValueError as exc:
            raise KnowledgeError(401, "unauthorized", "OGX 内部鉴权请求无效") from exc
        token = payload.get("api_key") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not impl.security.is_admin_token(token):
            raise KnowledgeError(401, "unauthorized", "OGX 原生接口需要 Admin Token")
        return {"principal": "knowledge-admin", "attributes": {"roles": ["admin"]}}

    @router.post(
        "/knowledge/v1/knowledge-bases",
        response_model=KnowledgeBaseResponse,
        status_code=201,
        summary="创建技术 KnowledgeBase",
    )
    async def create_knowledge_base(
        body: KnowledgeBaseCreateRequest,
        request: Request,
        response: Response,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
    ) -> KnowledgeBaseResponse:
        require_runtime(request)
        result = await impl.create_knowledge_base(body, idempotency_key)
        response.status_code = 200 if result.replayed else 201
        return result

    @router.get(
        "/knowledge/v1/knowledge-bases/{knowledge_base_id}",
        response_model=KnowledgeBaseResponse,
        summary="查询技术 KnowledgeBase",
    )
    async def get_knowledge_base(knowledge_base_id: str, request: Request) -> KnowledgeBaseResponse:
        require_runtime(request)
        return await impl.get_knowledge_base(knowledge_base_id)

    @router.get(
        "/knowledge/v1/knowledge-bases/{knowledge_base_id}/inference-config",
        response_model=KnowledgeBaseInferenceConfigResponse,
        summary="查询 KnowledgeBase 模型配置",
    )
    async def get_inference_config(
        knowledge_base_id: str,
        request: Request,
    ) -> KnowledgeBaseInferenceConfigResponse:
        require_admin(request)
        return await impl.get_inference_config(knowledge_base_id)

    @router.put(
        "/knowledge/v1/knowledge-bases/{knowledge_base_id}/embedding-config",
        response_model=EmbeddingConfigResponse,
        summary="更新 KnowledgeBase Embedding 配置",
    )
    async def put_embedding_config(
        knowledge_base_id: str,
        body: EmbeddingConfigPutRequest,
        request: Request,
    ) -> EmbeddingConfigResponse:
        require_admin(request)
        return await impl.put_embedding_config(knowledge_base_id, body)

    @router.put(
        "/knowledge/v1/knowledge-bases/{knowledge_base_id}/rerank-config",
        response_model=RerankConfigResponse,
        summary="更新或关闭 KnowledgeBase Rerank",
    )
    async def put_rerank_config(
        knowledge_base_id: str,
        body: RerankConfigPutRequest,
        request: Request,
    ) -> RerankConfigResponse:
        require_admin(request)
        return await impl.put_rerank_config(knowledge_base_id, body)

    @router.delete(
        "/knowledge/v1/knowledge-bases/{knowledge_base_id}",
        status_code=204,
        summary="删除技术 KnowledgeBase",
    )
    async def delete_knowledge_base(knowledge_base_id: str, request: Request) -> None:
        require_runtime(request)
        await impl.delete_knowledge_base(knowledge_base_id)

    @router.post(
        "/knowledge/v1/ingest",
        response_model=IngestResponse,
        status_code=202,
        summary="异步提交一个原始文件",
    )
    async def ingest(
        request: Request,
        response: Response,
        file: Annotated[UploadFile, File(description="原始文件二进制及文件名")],
        knowledge_base_id: Annotated[str, Form(description="逻辑知识库 ID，不是 Qdrant Collection ID")],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
        attributes: Annotated[
            str | None,
            Form(description="可选业务过滤属性，使用 JSON 对象字符串"),
        ] = None,
    ) -> IngestResponse:
        require_runtime(request)
        parsed_attributes = parse_attributes(attributes)
        content = await read_upload_with_size_limit(file, effective_max_upload_size_bytes)
        safe_file = PreReadUploadFile(content, filename=file.filename, content_type=file.content_type)
        result = await impl.ingest(safe_file, knowledge_base_id, parsed_attributes, idempotency_key)
        response.status_code = 200 if result.replayed else 202
        return result

    @router.post(
        "/knowledge/v1/ingest/batch",
        response_model=BatchIngestResponse,
        status_code=202,
        summary="异步提交多个原始文件",
    )
    async def batch_ingest(
        request: Request,
        response: Response,
        files: Annotated[list[UploadFile], File(description="同一 KnowledgeBase 的多个原始文件")],
        knowledge_base_id: Annotated[str, Form(description="逻辑知识库 ID，不是 Qdrant Collection ID")],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
        attributes: Annotated[
            str | None,
            Form(description="整批文件共用的可选业务过滤属性，使用 JSON 对象字符串"),
        ] = None,
    ) -> BatchIngestResponse:
        require_runtime(request)
        parsed_attributes = parse_attributes(attributes)
        result = await impl.batch_ingest(files, knowledge_base_id, parsed_attributes, idempotency_key)
        response.status_code = 200 if result.replayed else 202
        return result

    @router.get(
        "/knowledge/v1/operations/{operation_id}",
        response_model=OperationResponse,
        summary="查询异步导入状态",
    )
    async def get_ingest_operation(
        operation_id: str,
        request: Request,
    ) -> OperationResponse:
        require_runtime(request)
        return await impl.get_ingest_operation(operation_id)

    @router.post(
        "/knowledge/v1/operations/{operation_id}/retry",
        response_model=RetryOperationResponse,
        status_code=202,
        summary="重试最终失败的导入",
    )
    async def retry_ingest_operation(
        operation_id: str,
        request: Request,
        response: Response,
    ) -> RetryOperationResponse:
        require_runtime(request)
        result = await impl.retry_ingest_operation(operation_id)
        response.status_code = 200 if result.replayed else 202
        return result

    @router.post(
        "/knowledge/v1/knowledge-bases/{knowledge_base_id}/files/query",
        response_model=FileQueryResponse,
        summary="查询 KnowledgeBase 文件",
    )
    async def query_files(
        knowledge_base_id: str,
        body: FileQueryRequest,
        request: Request,
    ) -> FileQueryResponse:
        require_runtime(request)
        return await impl.query_files(knowledge_base_id, body)

    @router.get(
        "/knowledge/v1/knowledge-bases/{knowledge_base_id}/files/{file_id}",
        response_model=FileDetail,
        summary="查询 KnowledgeBase 文件详情",
    )
    async def get_file(knowledge_base_id: str, file_id: str, request: Request) -> FileDetail:
        require_runtime(request)
        return await impl.get_file(knowledge_base_id, file_id)

    @router.get(
        "/knowledge/v1/knowledge-bases/{knowledge_base_id}/files/{file_id}/download",
        response_class=Response,
        summary="下载 KnowledgeBase 原文件",
        responses={
            200: {
                "description": "上传时保存的原文件",
                "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
            }
        },
    )
    async def download_file(knowledge_base_id: str, file_id: str, request: Request) -> Response:
        """只允许通过所属 KnowledgeBase 下载原文件，避免全局 File ID 越界。"""

        require_runtime(request)
        return await impl.download_file(knowledge_base_id, file_id)

    @router.delete(
        "/knowledge/v1/knowledge-bases/{knowledge_base_id}/files/{file_id}",
        status_code=204,
        summary="删除 KnowledgeBase 文件",
    )
    async def delete_file(knowledge_base_id: str, file_id: str, request: Request) -> None:
        require_runtime(request)
        await impl.delete_file(knowledge_base_id, file_id)

    @router.post("/knowledge/v1/search", response_model=SearchResponse, summary="检索一个或多个逻辑知识库")
    async def search(body: SearchRequest, request: Request) -> SearchResponse:
        require_runtime(request)
        return await impl.search(body)

    # 产品 OpenAPI 只从 Knowledge 路由生成，不能把同进程的 OGX 原生接口暴露给调用方。
    product_openapi = build_product_openapi(router.routes)
    register_documentation_routes(router, product_openapi)

    # OGX 全局鉴权只保护其原生接口；统一 Knowledge API 继续使用自己的
    # Runtime/Admin 双 Token，以保持已经约定的稳定错误码和权限差异。
    # Scalar 页面和产品 OpenAPI 不含凭证，保持可直接打开；真实接口仍自行鉴权。
    for route in router.routes:
        if isinstance(route, APIRoute):
            route.openapi_extra = {**(route.openapi_extra or {}), PUBLIC_ROUTE_KEY: True}

    return router
