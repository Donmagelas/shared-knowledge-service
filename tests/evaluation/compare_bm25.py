"""在同一 Qdrant Collection 中比较 Jieba 与原生 multilingual BM25。

这是一组小型工程语料，只验证候选方案的可运行性和明显回归，不替代两侧真实文档上的效果评测。
"""

from __future__ import annotations

import json
import os
import time
import unicodedata
import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from statistics import mean

import jieba  # type: ignore[import-untyped]
import mmh3
from qdrant_client import QdrantClient, models


class LegacyJiebaBm25Encoder:
    """仅用于评测旧路线的 Jieba + FastEmbed-compatible 编码器。"""

    def tokenize(self, text: str) -> list[str]:
        normalized = unicodedata.normalize("NFKC", text).lower()
        raw_tokens: Iterable[str] = jieba.cut_for_search(normalized)
        return [token.strip() for token in raw_tokens if token.strip() and any(char.isalnum() for char in token)]

    def document_vector(self, text: str) -> models.SparseVector | None:
        tokens = self.tokenize(text)
        if not tokens:
            return None
        counts = Counter(tokens)
        document_length = len(tokens)
        weighted_ids: dict[int, float] = {}
        for token, frequency in counts.items():
            token_identifier = abs(mmh3.hash(token))
            weight = frequency * 2.2
            weight /= frequency + 1.2 * (0.25 + 0.75 * document_length / 256.0)
            weighted_ids[token_identifier] = weight
        indices = sorted(weighted_ids)
        return models.SparseVector(indices=indices, values=[weighted_ids[index] for index in indices])

    def query_vector(self, text: str) -> models.SparseVector | None:
        indices = sorted({abs(mmh3.hash(token)) for token in self.tokenize(text)})
        if not indices:
            return None
        return models.SparseVector(indices=indices, values=[1.0] * len(indices))


@dataclass(frozen=True, slots=True)
class EvaluationDocument:
    """一条带稳定标识的评测文档。"""

    identifier: str
    text: str


@dataclass(frozen=True, slots=True)
class EvaluationQuery:
    """一条查询及其唯一相关文档。"""

    text: str
    relevant_document: str


DOCUMENTS = (
    EvaluationDocument("refund", "退款申请需要订单编号、支付凭证和退款原因，审核通过后原路退回。"),
    EvaluationDocument("invoice", "开具电子发票需要订单编号、公司抬头和纳税人识别号。"),
    EvaluationDocument("leave", "员工请假需要提交请假单，由直属主管完成审批。"),
    EvaluationDocument("auth-401", "Cherry Studio 遇到 ERR_AUTH_401 时，应刷新访问令牌后重试。"),
    EvaluationDocument("qdrant-filter", "Qdrant 使用 Payload Filter 在 HNSW 检索阶段执行权限过滤。"),
    EvaluationDocument("warranty", "星云Pro7产品提供两年保修，售后需要设备序列号。"),
    EvaluationDocument("chunking", "Docling HybridChunker 按文档结构和 token 数量生成知识库切块。"),
    EvaluationDocument("deployment", "统一知识库由 OGX、PostgreSQL 和 Qdrant 三个服务组成。"),
)

QUERIES = (
    EvaluationQuery("退款需要哪些材料", "refund"),
    EvaluationQuery("支付凭证和退款原因", "refund"),
    EvaluationQuery("开电子发票要提供什么", "invoice"),
    EvaluationQuery("ERR_AUTH_401 怎么处理", "auth-401"),
    EvaluationQuery("Cherry Studio 访问令牌过期", "auth-401"),
    EvaluationQuery("HNSW 权限过滤", "qdrant-filter"),
    EvaluationQuery("星云Pro7保修多久", "warranty"),
    EvaluationQuery("Docling 如何切块", "chunking"),
    EvaluationQuery("统一知识库部署哪些服务", "deployment"),
)

NATIVE_BM25_OPTIONS = models.Bm25Config(
    tokenizer=models.TokenizerType.MULTILINGUAL,
    # Qdrant 1.18.2 使用 none 关闭默认英文词干与停用词处理；升级时需重新验证该选项。
    language="none",
)


def _metrics(rankings: list[list[str]]) -> dict[str, float]:
    """计算小型评测使用的 Top1、Recall@3 和 MRR。"""

    reciprocal_ranks: list[float] = []
    top1_hits = 0
    top3_hits = 0
    for query, ranked_ids in zip(QUERIES, rankings, strict=True):
        if ranked_ids and ranked_ids[0] == query.relevant_document:
            top1_hits += 1
        if query.relevant_document in ranked_ids[:3]:
            top3_hits += 1
        try:
            rank = ranked_ids.index(query.relevant_document) + 1
        except ValueError:
            reciprocal_ranks.append(0.0)
        else:
            reciprocal_ranks.append(1.0 / rank)
    query_count = len(QUERIES)
    return {
        "mrr": mean(reciprocal_ranks),
        "recall_at_3": top3_hits / query_count,
        "top1_accuracy": top1_hits / query_count,
    }


def _evaluate(client: QdrantClient, collection_name: str) -> dict[str, object]:
    """写入两套 Sparse Vector，逐查询记录排序、指标和客户端耗时。"""

    jieba_encoder = LegacyJiebaBm25Encoder()
    points: list[models.PointStruct] = []
    for point_id, document in enumerate(DOCUMENTS, start=1):
        jieba_vector = jieba_encoder.document_vector(document.text)
        if jieba_vector is None:
            raise RuntimeError(f"评测文档不能产生 Jieba Sparse Vector：{document.identifier}")
        points.append(
            models.PointStruct(
                id=point_id,
                vector={
                    "jieba": jieba_vector,
                    "multilingual": models.Document(
                        text=document.text,
                        model="qdrant/bm25",
                        options=NATIVE_BM25_OPTIONS,
                    ),
                },
                payload={"document_id": document.identifier, "text": document.text},
            )
        )
    client.upsert(collection_name=collection_name, points=points, wait=True)

    rankings: dict[str, list[list[str]]] = {"jieba": [], "multilingual": []}
    latencies_ms: dict[str, list[float]] = {"jieba": [], "multilingual": []}
    rows: list[dict[str, object]] = []
    for query in QUERIES:
        row: dict[str, object] = {"query": query.text, "relevant": query.relevant_document}
        query_vectors: dict[str, models.SparseVector | models.Document | None] = {
            "jieba": jieba_encoder.query_vector(query.text),
            "multilingual": models.Document(
                text=query.text,
                model="qdrant/bm25",
                options=NATIVE_BM25_OPTIONS,
            ),
        }
        for candidate, query_vector in query_vectors.items():
            if query_vector is None:
                ranked_ids: list[str] = []
                elapsed_ms = 0.0
            else:
                started = time.perf_counter()
                result = client.query_points(
                    collection_name=collection_name,
                    query=query_vector,
                    using=candidate,
                    limit=5,
                    with_payload=True,
                )
                elapsed_ms = (time.perf_counter() - started) * 1000
                ranked_ids = [str(point.payload["document_id"]) for point in result.points if point.payload]
            rankings[candidate].append(ranked_ids)
            latencies_ms[candidate].append(elapsed_ms)
            row[candidate] = ranked_ids
        rows.append(row)

    return {
        "corpus_size": len(DOCUMENTS),
        "queries": rows,
        "results": {
            candidate: {
                **_metrics(candidate_rankings),
                "mean_client_latency_ms": mean(latencies_ms[candidate]),
            }
            for candidate, candidate_rankings in rankings.items()
        },
    }


def main() -> int:
    """执行评测并始终清理唯一命名的临时 Collection。"""

    collection_name = f"bm25_evaluation_{uuid.uuid4().hex}"
    client = QdrantClient(
        url=os.environ.get("QDRANT_INTEGRATION_URL", "http://127.0.0.1:6333"),
        trust_env=False,
        check_compatibility=False,
    )
    try:
        client.create_collection(
            collection_name=collection_name,
            vectors_config={},
            sparse_vectors_config={
                "jieba": models.SparseVectorParams(modifier=models.Modifier.IDF),
                "multilingual": models.SparseVectorParams(modifier=models.Modifier.IDF),
            },
        )
        summary = _evaluate(client, collection_name)
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    finally:
        if client.collection_exists(collection_name):
            client.delete_collection(collection_name)
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
