"""Knowledge API 的稳定错误、请求 ID 与服务间鉴权。"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from ogx.log import get_logger
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import Response

log = get_logger(name=__name__, category="providers")


class KnowledgeError(Exception):
    """带稳定机器码的预期 API 错误。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def request_id_for(request: Request) -> str:
    """复用格式安全的调用方 ID，否则生成服务端请求 ID。"""

    existing = getattr(request.state, "knowledge_request_id", None)
    if isinstance(existing, str):
        return existing
    candidate = request.headers.get("X-Request-ID", "").strip()
    if (
        candidate
        and len(candidate) <= 128
        and all(character.isalnum() or character in "-_." for character in candidate)
    ):
        request_id = candidate
    else:
        request_id = f"req_{uuid.uuid4().hex}"
    request.state.knowledge_request_id = request_id
    return request_id


def error_response(request: Request, error: KnowledgeError) -> JSONResponse:
    """构造不泄露内部实现的统一错误信封。"""

    request_id = request_id_for(request)
    return JSONResponse(
        status_code=error.status_code,
        headers={"X-Request-ID": request_id},
        content={
            "error": {
                "code": error.code,
                "message": error.message,
                "details": error.details,
            },
            "request_id": request_id,
        },
    )


class StableKnowledgeRoute(APIRoute):
    """把路由执行和 FastAPI 校验错误统一转换为稳定信封。"""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            request_id = request_id_for(request)
            try:
                response = await original(request)
                response.headers["X-Request-ID"] = request_id
                return response
            except KnowledgeError as exc:
                return error_response(request, exc)
            except RequestValidationError as exc:
                # FastAPI 的版本化错误对象可能包含整个请求输入，只保留安全字段。
                details = {
                    "issues": [
                        {"type": issue.get("type"), "loc": issue.get("loc"), "msg": issue.get("msg")}
                        for issue in exc.errors()
                    ]
                }
                return error_response(request, KnowledgeError(422, "invalid_request", "请求字段不合法", details))
            except HTTPException as exc:
                # 上传大小等框架错误也必须经过稳定错误协议。
                code = "payload_too_large" if exc.status_code == 413 else "request_rejected"
                message = "上传文件超过部署限制" if exc.status_code == 413 else "请求被拒绝"
                return error_response(request, KnowledgeError(exc.status_code, code, message))
            except (ResponseHandlingException, UnexpectedResponse, SQLAlchemyError) as exc:
                # 明确的外部存储故障属于暂时不可用；其他未知异常仍保持 500，避免掩盖程序缺陷。
                log.exception(
                    "Knowledge storage dependency unavailable",
                    request_id=request_id,
                    error_type=type(exc).__name__,
                )
                return error_response(request, KnowledgeError(503, "storage_unavailable", "知识库存储暂时不可用"))
            except Exception as exc:
                # 堆栈只进入服务日志，响应不能包含内部路径、SQL 或上游正文。
                log.exception(
                    "Unhandled Knowledge API error",
                    request_id=request_id,
                    error_type=type(exc).__name__,
                )
                return error_response(request, KnowledgeError(500, "internal_error", "知识库服务发生内部错误"))

        return handler


@dataclass(frozen=True)
class ApiSecurity:
    """部署注入的两个静态服务 Token。"""

    runtime_token: str
    admin_token: str

    def is_admin_token(self, token: str) -> bool:
        """常量时间判断 Admin Token，供 OGX 原生路由的内部鉴权回调复用。"""

        return bool(token) and secrets.compare_digest(token, self.admin_token)

    def require(self, request: Request, *, admin: bool = False) -> None:
        """Admin 是 Runtime 超集；Runtime 调用 Admin 接口返回 403。"""

        authorization = request.headers.get("Authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token:
            raise KnowledgeError(401, "unauthorized", "缺少有效的服务 Token")
        if self.is_admin_token(token):
            return
        if secrets.compare_digest(token, self.runtime_token):
            if admin:
                raise KnowledgeError(403, "admin_token_required", "该接口需要 Admin Token")
            return
        raise KnowledgeError(401, "unauthorized", "缺少有效的服务 Token")
