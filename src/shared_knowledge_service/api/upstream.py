"""KnowledgeBase 模型 URL 安全校验与精确配置探针。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .errors import KnowledgeError


@dataclass(frozen=True)
class InferenceUrlPolicy:
    """部署方显式允许的内部 Host 与 HTTP Host。"""

    allowed_hosts: frozenset[str] = field(default_factory=frozenset)
    http_allowed_hosts: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_csv(
        cls,
        *,
        allowed_hosts: str | None = None,
        http_allowed_hosts: str | None = None,
    ) -> InferenceUrlPolicy:
        def normalize(value: str | None) -> frozenset[str]:
            if value is None:
                return frozenset()
            return frozenset(item.strip().lower() for item in value.split(",") if item.strip())

        return cls(allowed_hosts=normalize(allowed_hosts), http_allowed_hosts=normalize(http_allowed_hosts))

    async def normalize_and_validate(self, value: str) -> str:
        """默认只允许公网 HTTPS；内部地址必须由部署配置放行。"""

        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise KnowledgeError(422, "invalid_inference_url", "模型 Base URL 必须是绝对 HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise KnowledgeError(422, "invalid_inference_url", "模型 Base URL 不能包含凭证、query 或 fragment")
        host = parsed.hostname.lower()
        if parsed.scheme == "http" and host not in self.http_allowed_hosts:
            raise KnowledgeError(422, "insecure_inference_url", "模型 Base URL 默认必须使用 HTTPS")
        if host not in self.allowed_hosts:
            addresses = await self._resolve(host, parsed.port)
            if any(self._is_blocked(address) for address in addresses):
                raise KnowledgeError(422, "blocked_inference_url", "模型 Base URL 解析到受保护的网络地址")
        netloc = host
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        normalized_path = parsed.path.rstrip("/")
        return urlunsplit((parsed.scheme, netloc, normalized_path, "", ""))

    @staticmethod
    async def _resolve(host: str, port: int | None) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            try:
                records = await asyncio.to_thread(socket.getaddrinfo, host, port or 443, type=socket.SOCK_STREAM)
            except socket.gaierror as exc:
                raise KnowledgeError(503, "inference_host_unavailable", "模型服务域名暂时无法解析") from exc
            return {ipaddress.ip_address(record[4][0]) for record in records}
        return {literal}

    @staticmethod
    def _is_blocked(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return bool(
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        )


def endpoint(base_url: str, operation: str) -> str:
    """在保留 `/v1` 等部署路径的前提下拼接模型操作。"""

    return f"{base_url.rstrip('/')}/{operation.lstrip('/')}"


async def post_model_request(
    *,
    policy: InferenceUrlPolicy,
    base_url: str,
    api_key: str,
    operation: str,
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    """执行不跟随重定向的安全模型请求，且不把上游正文写入错误。"""

    normalized = await policy.normalize_and_validate(base_url)
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
            response = await client.post(
                endpoint(normalized, operation),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise KnowledgeError(503, "inference_unavailable", "模型服务暂时不可用") from exc
    if response.is_redirect:
        raise KnowledgeError(502, "inference_redirect_rejected", "模型服务返回了不允许的重定向")
    if not response.is_success:
        raise KnowledgeError(
            502,
            "inference_rejected",
            "模型服务拒绝了请求",
            {"upstream_status": response.status_code},
        )
    try:
        result: Any = response.json()
    except ValueError as exc:
        raise KnowledgeError(502, "invalid_inference_response", "模型服务返回了非法 JSON") from exc
    if not isinstance(result, dict):
        raise KnowledgeError(502, "invalid_inference_response", "模型服务返回结构不合法")
    return result


async def probe_embedding(
    *,
    policy: InferenceUrlPolicy,
    base_url: str,
    api_key: str,
    model_id: str,
    dimension: int | None,
    timeout_seconds: float,
) -> int:
    """探测确切模型并返回实际维度；调用方未指定时不发送 dimensions。"""

    payload: dict[str, Any] = {"model": model_id, "input": ["knowledge service probe"]}
    if dimension is not None:
        payload["dimensions"] = dimension
    result = await post_model_request(
        policy=policy,
        base_url=base_url,
        api_key=api_key,
        operation="embeddings",
        payload=payload,
        timeout_seconds=timeout_seconds,
    )
    data = result.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise KnowledgeError(502, "invalid_embedding_response", "Embedding 服务返回结构不合法")
    vector = data[0].get("embedding")
    if not isinstance(vector, list) or not vector or not all(isinstance(item, int | float) for item in vector):
        raise KnowledgeError(502, "invalid_embedding_response", "Embedding 服务返回向量不合法")
    actual_dimension = len(vector)
    if actual_dimension > 65_536:
        raise KnowledgeError(502, "invalid_embedding_response", "Embedding 服务返回维度超过服务上限")
    if dimension is not None and actual_dimension != dimension:
        raise KnowledgeError(
            502,
            "embedding_dimension_mismatch",
            "Embedding 服务返回维度与配置不一致",
            {"expected_dimension": dimension, "actual_dimension": actual_dimension},
        )
    return actual_dimension


async def probe_rerank(
    *,
    policy: InferenceUrlPolicy,
    base_url: str,
    api_key: str,
    model_id: str,
    timeout_seconds: float,
) -> None:
    """只探测调用方提交的确切 Rerank 模型。"""

    result = await post_model_request(
        policy=policy,
        base_url=base_url,
        api_key=api_key,
        operation="rerank",
        payload={
            "model": model_id,
            "query": "退款材料",
            "documents": ["公司食堂菜单", "退款需要订单号"],
            "top_n": 2,
        },
        timeout_seconds=timeout_seconds,
    )
    rows = result.get("results", result.get("data"))
    if not isinstance(rows, list) or not rows:
        raise KnowledgeError(502, "invalid_rerank_response", "Rerank 服务返回结构不合法")
    for row in rows:
        if not isinstance(row, dict) or "index" not in row or "relevance_score" not in row:
            raise KnowledgeError(502, "invalid_rerank_response", "Rerank 服务返回结构不合法")
