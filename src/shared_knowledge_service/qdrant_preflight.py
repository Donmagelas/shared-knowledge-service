"""Qdrant Sparse IDF、Payload Filter 和原生 RRF 的前置探针。"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import uuid4

from qdrant_client import QdrantClient, models

from .provider.bm25 import native_bm25_document


class QdrantPreflightError(RuntimeError):
    """表示目标 Qdrant 不满足 MVP 所需特性。"""


@dataclass(frozen=True, slots=True)
class QdrantProbeConfig:
    """Qdrant 探针连接配置。"""

    url: str = "http://localhost:6333"
    api_key: str | None = None
    timeout_seconds: int = 30

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> QdrantProbeConfig:
        raw_timeout = values.get("QDRANT_TIMEOUT_SECONDS", "30")
        try:
            timeout_seconds = int(raw_timeout)
        except ValueError as exc:
            raise QdrantPreflightError("QDRANT_TIMEOUT_SECONDS 必须是正整数") from exc
        if timeout_seconds <= 0:
            raise QdrantPreflightError("QDRANT_TIMEOUT_SECONDS 必须是正整数")

        return cls(
            url=values.get("QDRANT_URL", "http://localhost:6333").rstrip("/"),
            api_key=values.get("QDRANT_API_KEY") or None,
            timeout_seconds=timeout_seconds,
        )


def probe_qdrant(config: QdrantProbeConfig) -> dict[str, int | str]:
    """验证 MVP 依赖的 Qdrant 查询能力，并始终清理临时 Collection。"""

    collection_name = f"shared_knowledge_preflight_{uuid4().hex}"
    # Qdrant 通常位于本机或部署内网，不能让开发机的通用 SOCKS/HTTP 代理改写探针目标。
    client = QdrantClient(
        url=config.url,
        api_key=config.api_key,
        timeout=config.timeout_seconds,
        trust_env=False,
        # 探针会主动读取版本并执行所需特性，不依赖客户端初始化时的额外联网检查。
        check_compatibility=False,
    )
    try:
        version = client.info().version
        client.create_collection(
            collection_name=collection_name,
            vectors_config={"dense": models.VectorParams(size=3, distance=models.Distance.COSINE)},
            sparse_vectors_config={
                "bm25": models.SparseVectorParams(modifier=models.Modifier.IDF),
            },
        )
        allowed_bm25 = native_bm25_document("alpha beta")
        denied_bm25 = native_bm25_document("alpha")
        query_bm25 = native_bm25_document("alpha")
        if allowed_bm25 is None or denied_bm25 is None or query_bm25 is None:
            raise QdrantPreflightError("Qdrant 原生 BM25 探针文本不能产生文档输入")
        client.upsert(
            collection_name=collection_name,
            wait=True,
            points=[
                models.PointStruct(
                    id=1,
                    vector={
                        "dense": [1.0, 0.0, 0.0],
                        "bm25": allowed_bm25,
                    },
                    payload={"vector_store_id": "allowed", "content": "alpha beta"},
                ),
                models.PointStruct(
                    id=2,
                    vector={
                        "dense": [1.0, 0.0, 0.0],
                        "bm25": denied_bm25,
                    },
                    payload={"vector_store_id": "denied", "content": "alpha"},
                ),
            ],
        )

        allowed_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="vector_store_id",
                    match=models.MatchValue(value="allowed"),
                )
            ]
        )
        result = client.query_points(
            collection_name=collection_name,
            prefetch=[
                models.Prefetch(query=[1.0, 0.0, 0.0], using="dense", filter=allowed_filter, limit=2),
                models.Prefetch(
                    query=query_bm25,
                    using="bm25",
                    filter=allowed_filter,
                    limit=2,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=2,
            with_payload=True,
        )
        if [point.id for point in result.points] != [1]:
            raise QdrantPreflightError("Qdrant RRF 查询没有正确执行 Payload Filter")
        return {"matched_points": len(result.points), "qdrant_version": version}
    except QdrantPreflightError:
        raise
    except Exception as exc:
        # 不回显连接 URL、API Key 或远程响应正文。
        raise QdrantPreflightError(f"Qdrant 特性探针失败：{type(exc).__name__}") from exc
    finally:
        try:
            if client.collection_exists(collection_name):
                client.delete_collection(collection_name)
        except Exception:
            # 原始异常比清理异常更重要；残留 Collection 使用唯一前缀，便于人工清理。
            pass
        client.close()


def main() -> int:
    """执行命令行探针。"""

    try:
        config = QdrantProbeConfig.from_mapping(os.environ)
        summary = probe_qdrant(config)
    except QdrantPreflightError as exc:
        print(f"Qdrant 前置探针失败：{exc}", file=sys.stderr)
        return 2

    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
