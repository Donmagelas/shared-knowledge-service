"""使用部署 API 对真实文档执行 Dense、BM25 与 Hybrid 检索评测。

脚本不内置或复制业务文档。调用方通过参数传入本地路径；输出只包含文档
标识、排名、指标和耗时，不输出原文、凭证或完整向量。
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True, slots=True)
class CorpusDocument:
    """一份只在本地读取的评测文档。"""

    group: str
    document_id: str
    path: Path


@dataclass(frozen=True, slots=True)
class EvaluationQuery:
    """一条查询及其唯一相关文档标注。"""

    expected_document_id: str
    text: str


def _parse_document(raw: str) -> CorpusDocument:
    try:
        group, document_id, path = raw.split(":", 2)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("document 必须为 GROUP:DOCUMENT_ID:PATH") from exc
    normalized_path = Path(path).expanduser().resolve()
    if not group or not document_id or not normalized_path.is_file():
        raise argparse.ArgumentTypeError(f"文档参数无效或文件不存在：{raw}")
    return CorpusDocument(group=group, document_id=document_id, path=normalized_path)


def _parse_query(raw: str) -> EvaluationQuery:
    try:
        expected_document_id, text = raw.split(":", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("query 必须为 DOCUMENT_ID:QUERY") from exc
    if not expected_document_id or not text.strip():
        raise argparse.ArgumentTypeError(f"查询参数无效：{raw}")
    return EvaluationQuery(expected_document_id=expected_document_id, text=text.strip())


def _create_knowledge_base(client: httpx.Client, group: str) -> str:
    response = client.post(
        "/v1/vector_stores",
        json={
            "name": f"live-eval-{group}-{uuid.uuid4().hex[:8]}",
            "metadata": {"evaluation": True, "tenant_id": "live-evaluation"},
        },
    )
    response.raise_for_status()
    return str(response.json()["id"])


def _ingest_document(client: httpx.Client, knowledge_base_id: str, document: CorpusDocument) -> tuple[str, float]:
    started_at = time.perf_counter()
    # MDX 以 Markdown 内容送入 Docling；避免让扩展名承担产品语义。
    upload_name = f"{document.document_id}.md" if document.path.suffix.lower() == ".mdx" else document.path.name
    response = client.post(
        "/knowledge/v1/ingest",
        files={"file": (upload_name, document.path.read_bytes(), "text/markdown")},
        data={
            "knowledge_base_id": knowledge_base_id,
            "attributes": json.dumps(
                {
                    "eval_document_id": document.document_id,
                    "source_group": document.group,
                },
                ensure_ascii=False,
            ),
        },
    )
    response.raise_for_status()
    body = response.json()
    if response.status_code != 202 or body.get("status") != "processing":
        raise RuntimeError(f"异步导入未被可靠接收：{document.document_id}: {body}")

    operation_id = str(body["operation_id"])
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        operation = client.get(
            f"/knowledge/v1/operations/{operation_id}",
        )
        operation.raise_for_status()
        operation_body = operation.json()
        if operation_body["status"] == "completed":
            break
        if operation_body["status"] in {"failed", "cancelled"}:
            raise RuntimeError(f"文档导入失败：{document.document_id}: {operation_body.get('last_error')}")
        time.sleep(0.1)
    else:
        raise TimeoutError(f"文档导入超时：{document.document_id}")
    return str(body["file_id"]), round((time.perf_counter() - started_at) * 1000, 2)


def _search(
    client: httpx.Client,
    knowledge_base_ids: list[str],
    query: EvaluationQuery,
    mode: str,
    limit: int,
) -> tuple[list[str], float]:
    started_at = time.perf_counter()
    response = client.post(
        "/knowledge/v1/search",
        json={
            "query": query.text,
            "knowledge_base_ids": knowledge_base_ids,
            "mode": mode,
            "limit": limit,
        },
    )
    response.raise_for_status()
    latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
    ranked_document_ids: list[str] = []
    for hit in response.json()["hits"]:
        document_id = str(hit.get("attributes", {}).get("eval_document_id", ""))
        if document_id and document_id not in ranked_document_ids:
            ranked_document_ids.append(document_id)
    return ranked_document_ids, latency_ms


def _evaluate_mode(
    client: httpx.Client,
    knowledge_base_ids: list[str],
    queries: list[EvaluationQuery],
    mode: str,
    limit: int,
) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    reciprocal_ranks: list[float] = []
    latencies: list[float] = []
    for query in queries:
        ranked_document_ids, latency_ms = _search(client, knowledge_base_ids, query, mode, limit)
        latencies.append(latency_ms)
        try:
            rank = ranked_document_ids.index(query.expected_document_id) + 1
        except ValueError:
            rank = None
        reciprocal_ranks.append(0.0 if rank is None else 1.0 / rank)
        cases.append(
            {
                "expected": query.expected_document_id,
                "query": query.text,
                "rank": rank,
                "top_documents": ranked_document_ids[:3],
                "latency_ms": latency_ms,
            }
        )

    query_count = len(queries)
    return {
        "mode": mode,
        "query_count": query_count,
        "top1": sum(case["rank"] == 1 for case in cases) / query_count,
        "recall_at_3": sum(case["rank"] is not None and case["rank"] <= 3 for case in cases) / query_count,
        "mrr": sum(reciprocal_ranks) / query_count,
        "latency_ms_mean": round(statistics.mean(latencies), 2),
        "latency_ms_p50": round(statistics.median(latencies), 2),
        "cases": cases,
    }


def _delete_resources(client: httpx.Client, knowledge_base_ids: list[str], file_ids: list[str]) -> None:
    for knowledge_base_id in knowledge_base_ids:
        response = client.delete(f"/v1/vector_stores/{knowledge_base_id}")
        if response.status_code not in {200, 404}:
            response.raise_for_status()
    for file_id in file_ids:
        response = client.delete(f"/v1/files/{file_id}")
        if response.status_code not in {200, 404}:
            response.raise_for_status()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8321")
    parser.add_argument("--document", action="append", type=_parse_document, required=True)
    parser.add_argument("--query", action="append", type=_parse_query, required=True)
    parser.add_argument("--mode", action="append", choices=("dense", "bm25", "hybrid"))
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--keep", action="store_true", help="保留本次评测创建的逻辑知识库和文件")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    documents: list[CorpusDocument] = args.document
    queries: list[EvaluationQuery] = args.query
    document_ids = {document.document_id for document in documents}
    missing_labels = sorted({query.expected_document_id for query in queries} - document_ids)
    if missing_labels:
        raise SystemExit(f"查询标注引用了未导入文档：{', '.join(missing_labels)}")
    if args.limit < 3:
        raise SystemExit("limit 至少为 3，才能计算 Recall@3")

    grouped_documents: dict[str, list[CorpusDocument]] = defaultdict(list)
    for document in documents:
        grouped_documents[document.group].append(document)

    client = httpx.Client(base_url=args.base_url, timeout=300, trust_env=False)
    knowledge_base_ids: list[str] = []
    file_ids: list[str] = []
    ingest_results: list[dict[str, Any]] = []
    try:
        for group, group_documents in grouped_documents.items():
            knowledge_base_id = _create_knowledge_base(client, group)
            knowledge_base_ids.append(knowledge_base_id)
            for document in group_documents:
                file_id, latency_ms = _ingest_document(client, knowledge_base_id, document)
                file_ids.append(file_id)
                ingest_results.append(
                    {
                        "document_id": document.document_id,
                        "group": group,
                        "latency_ms": latency_ms,
                    }
                )

        modes = args.mode or ["dense", "bm25", "hybrid"]
        results = [_evaluate_mode(client, knowledge_base_ids, queries, mode, args.limit) for mode in modes]
        print(
            json.dumps(
                {
                    "document_count": len(documents),
                    "knowledge_base_count": len(knowledge_base_ids),
                    "ingest": ingest_results,
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        if not args.keep:
            _delete_resources(client, knowledge_base_ids, file_ids)
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
