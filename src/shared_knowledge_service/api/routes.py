"""统一 Knowledge API 的 FastAPI 路由。"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from ogx_api.common.upload_limits import (
    DEFAULT_MAX_UPLOAD_SIZE_BYTES,
    PreReadUploadFile,
    read_upload_with_size_limit,
)
from pydantic import ValidationError

from .models import AttributeValue, IngestOperationResponse, IngestResponse, SearchRequest, SearchResponse
from .protocol import KnowledgeApi


def parse_attributes(value: str | None) -> dict[str, AttributeValue]:
    """解析 multipart 表单中的 JSON attributes。"""

    if value is None or not value.strip():
        return {}
    try:
        parsed: Any = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"attributes 必须是合法 JSON：{exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("attributes 必须是 JSON 对象")
    return parsed


def create_router(
    impl: KnowledgeApi,
    max_upload_size_bytes: int = DEFAULT_MAX_UPLOAD_SIZE_BYTES,
) -> APIRouter:
    """创建挂载在 OGX 同一 FastAPI 进程中的产品接口。"""

    router = APIRouter(prefix="/knowledge/v1", tags=["Knowledge"])

    @router.post(
        "/ingest",
        response_model=IngestResponse,
        status_code=202,
        summary="异步提交一个原始文件",
    )
    async def ingest(
        file: Annotated[UploadFile, File(description="原始文件二进制及文件名")],
        knowledge_base_id: Annotated[str, Form(description="逻辑知识库 ID，不是 Qdrant Collection ID")],
        attributes: Annotated[
            str | None,
            Form(description="可选业务过滤属性，使用 JSON 对象字符串"),
        ] = None,
    ) -> IngestResponse:
        try:
            parsed_attributes = parse_attributes(attributes)
            content = await read_upload_with_size_limit(file, max_upload_size_bytes)
            safe_file = PreReadUploadFile(content, filename=file.filename, content_type=file.content_type)
            return await impl.ingest(safe_file, knowledge_base_id, parsed_attributes)
        except (ValidationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get(
        "/knowledge-bases/{knowledge_base_id}/operations/{operation_id}",
        response_model=IngestOperationResponse,
        summary="查询异步导入状态",
    )
    async def get_ingest_operation(
        knowledge_base_id: str,
        operation_id: str,
    ) -> IngestOperationResponse:
        try:
            return await impl.get_ingest_operation(knowledge_base_id, operation_id)
        except (ValidationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/search", response_model=SearchResponse, summary="检索一个或多个逻辑知识库")
    async def search(request: SearchRequest) -> SearchResponse:
        try:
            return await impl.search(request)
        except (ValidationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return router
