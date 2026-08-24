"""Embedding 服务的只读前置探针。"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx


class PreflightError(RuntimeError):
    """表示配置或远程服务不满足 Embedding 前置条件。"""


@dataclass(frozen=True, slots=True)
class EmbeddingProbeConfig:
    """执行 Embedding 探针所需的最小配置。"""

    base_url: str
    api_key: str
    model: str
    expected_dimension: int
    batch_size: int = 2
    timeout_seconds: float = 30.0

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> EmbeddingProbeConfig:
        """从环境变量兼容的键值表读取并校验配置。"""

        required_names = (
            "EMBEDDING_BASE_URL",
            "EMBEDDING_API_KEY",
            "EMBEDDING_MODEL",
            "EMBEDDING_DIMENSION",
        )
        missing = [name for name in required_names if not values.get(name, "").strip()]
        if missing:
            raise PreflightError(f"缺少配置：{', '.join(missing)}")

        base_url = values["EMBEDDING_BASE_URL"].strip().rstrip("/")
        parsed_url = httpx.URL(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.host:
            raise PreflightError("EMBEDDING_BASE_URL 必须是有效的 HTTP(S) URL")

        batch_size = _parse_positive_int(values.get("EMBEDDING_PROBE_BATCH_SIZE", "2"), "EMBEDDING_PROBE_BATCH_SIZE")
        expected_dimension = _parse_positive_int(values["EMBEDDING_DIMENSION"], "EMBEDDING_DIMENSION")
        timeout_seconds = _parse_positive_float(
            values.get("EMBEDDING_TIMEOUT_SECONDS", "30"),
            "EMBEDDING_TIMEOUT_SECONDS",
        )
        return cls(
            base_url=base_url,
            api_key=values["EMBEDDING_API_KEY"].strip(),
            model=values["EMBEDDING_MODEL"].strip(),
            expected_dimension=expected_dimension,
            batch_size=batch_size,
            timeout_seconds=timeout_seconds,
        )

    @property
    def endpoint(self) -> str:
        """返回 OpenAI-compatible embeddings endpoint。"""

        if self.base_url.endswith("/embeddings"):
            return self.base_url
        return f"{self.base_url}/embeddings"


def probe_embedding(
    config: EmbeddingProbeConfig,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, int | float | str]:
    """调用一次 Embedding 服务并返回不含凭证和 Endpoint 的摘要。"""

    inputs = [f"shared knowledge embedding preflight {index}" for index in range(config.batch_size)]
    started_at = time.perf_counter()
    try:
        # 前置探针验证的是知识库服务到模型服务的直连能力，不应被开发机的
        # HTTP_PROXY/ALL_PROXY 意外改写；生产代理应由部署网络显式提供。
        with httpx.Client(timeout=config.timeout_seconds, transport=transport, trust_env=False) as client:
            response = client.post(
                config.endpoint,
                headers={"Authorization": f"Bearer {config.api_key}"},
                json={"model": config.model, "input": inputs},
            )
    except httpx.TimeoutException as exc:
        raise PreflightError("Embedding 请求超时") from exc
    except httpx.RequestError as exc:
        # 不回显 URL，避免把内部 Endpoint 写入日志。
        raise PreflightError("无法连接 Embedding 服务") from exc

    elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
    if not response.is_success:
        # 不回显响应正文，因为第三方网关可能把请求信息写进错误详情。
        raise PreflightError(f"Embedding 服务返回 HTTP {response.status_code}")

    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise PreflightError("Embedding 服务没有返回 JSON") from exc

    vectors = _extract_vectors(payload, expected_count=config.batch_size)
    dimensions = {len(vector) for vector in vectors}
    if len(dimensions) != 1:
        raise PreflightError("Embedding 返回的向量维度不一致")
    actual_dimension = dimensions.pop()
    if actual_dimension != config.expected_dimension:
        raise PreflightError(
            f"Embedding 返回维度与配置不一致：配置 {config.expected_dimension}，实际 {actual_dimension}"
        )

    return {
        "accepted_batch_size": config.batch_size,
        "dimension": actual_dimension,
        "latency_ms": elapsed_ms,
        "model": config.model,
        "returned_vectors": len(vectors),
    }


def _extract_vectors(payload: Any, *, expected_count: int) -> list[list[int | float]]:
    """校验 OpenAI-compatible 响应中的向量列表。"""

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise PreflightError("Embedding 响应缺少 data 列表")

    vectors: list[list[int | float]] = []
    for item in payload["data"]:
        if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
            raise PreflightError("Embedding 响应缺少 embedding 数组")
        vector = item["embedding"]
        if not vector or any(not isinstance(value, int | float) for value in vector):
            raise PreflightError("Embedding 向量必须是非空数值数组")
        vectors.append(vector)

    if len(vectors) != expected_count:
        raise PreflightError(f"Embedding 返回数量不正确：期望 {expected_count}，实际 {len(vectors)}")
    return vectors


def _parse_positive_int(raw_value: str, name: str) -> int:
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise PreflightError(f"{name} 必须是正整数") from exc
    if value <= 0:
        raise PreflightError(f"{name} 必须是正整数")
    return value


def _parse_positive_float(raw_value: str, name: str) -> float:
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise PreflightError(f"{name} 必须是正数") from exc
    if value <= 0:
        raise PreflightError(f"{name} 必须是正数")
    return value


def main() -> int:
    """执行命令行探针。"""

    try:
        config = EmbeddingProbeConfig.from_mapping(os.environ)
        summary = probe_embedding(config)
    except PreflightError as exc:
        print(f"Embedding 前置探针失败：{exc}", file=sys.stderr)
        return 2

    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
