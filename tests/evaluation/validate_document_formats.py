"""通过统一 Knowledge API 验证首批非 OCR 文档格式并采集容器资源。

这不是 Docling 的静态格式清单，而是对当前固定镜像执行真实的异步导入、
Dense/BM25/Hybrid 检索和清理。单个格式失败不会中断其余格式，便于得到完整矩阵。
"""

from __future__ import annotations

import argparse
import csv
import json
import platform
import statistics
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from simulate_product_usage import DockerStatsSampler, ResourceSample

RUNTIME_TOKEN = "runtime-e2e-at-least-sixteen"
ADMIN_TOKEN = "admin-e2e-at-least-sixteen"
TERMINAL_OPERATION_STATUSES = {"completed", "failed", "cancelled"}


@dataclass(frozen=True, slots=True)
class FormatCase:
    """一份格式样本及其可判定的检索事实。"""

    id: str
    category: str
    path: Path
    mime_type: str
    marker: str
    question: str
    expected_answer: str
    features: tuple[str, ...]


@dataclass(slots=True)
class FormatResult:
    """单个文件从上传到三种检索模式的完整结果。"""

    id: str
    category: str
    filename: str
    status: str = "pending"
    operation_status: str | None = None
    operation_id: str | None = None
    file_id: str | None = None
    ingest_latency_ms: float | None = None
    search_latency_ms: dict[str, float] = field(default_factory=dict)
    mode_passed: dict[str, bool] = field(default_factory=dict)
    answer_probe_passed: bool = False
    error: str | None = None


def _headers(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


def _load_cases(manifest_path: Path) -> list[FormatCase]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    base_dir = manifest_path.parent
    cases: list[FormatCase] = []
    for raw in payload["cases"]:
        path = (base_dir / raw["filename"]).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"格式样本不存在：{path}")
        cases.append(
            FormatCase(
                id=str(raw["id"]),
                category=str(raw["category"]),
                path=path,
                mime_type=str(raw["mime_type"]),
                marker=str(raw["marker"]),
                question=str(raw["question"]),
                expected_answer=str(raw["expected_answer"]),
                features=tuple(map(str, raw.get("features", []))),
            )
        )
    return cases


def _configure_embedding(client: httpx.Client, tenant_id: str) -> None:
    response = client.put(
        f"/knowledge/v1/tenants/{tenant_id}/embedding-config",
        headers=_headers(ADMIN_TOKEN),
        json={
            "base_url": "http://embedding-stub:18080/v1",
            "api_key": "document-format-evaluation-only",
            "model_id": "deterministic-test",
            "dimension": 3,
        },
    )
    response.raise_for_status()


def _create_knowledge_base(client: httpx.Client, tenant_id: str, run_id: str) -> str:
    response = client.post(
        "/knowledge/v1/knowledge-bases",
        headers=_headers(RUNTIME_TOKEN, **{"Idempotency-Key": f"formats-create-{run_id}"}),
        json={"tenant_id": tenant_id},
    )
    response.raise_for_status()
    return str(response.json()["knowledge_base_id"])


def _wait_operation(
    client: httpx.Client,
    knowledge_base_id: str,
    operation_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = client.get(
            f"/knowledge/v1/knowledge-bases/{knowledge_base_id}/operations/{operation_id}",
            headers=_headers(RUNTIME_TOKEN),
        )
        response.raise_for_status()
        body = response.json()
        if body["status"] in TERMINAL_OPERATION_STATUSES:
            return dict(body)
        time.sleep(0.2)
    raise TimeoutError(f"Operation {operation_id} 在 {timeout_seconds:.0f} 秒内未结束")


def _ingest_case(
    client: httpx.Client,
    knowledge_base_id: str,
    case: FormatCase,
    run_id: str,
    timeout_seconds: float,
) -> tuple[dict[str, Any], float]:
    started_at = time.perf_counter()
    response = client.post(
        "/knowledge/v1/ingest",
        headers=_headers(RUNTIME_TOKEN, **{"Idempotency-Key": f"formats-{run_id}-{case.id}"}),
        data={
            "knowledge_base_id": knowledge_base_id,
            "attributes": json.dumps(
                {
                    "fixture_set": "non_ocr_batch_1",
                    "fixture_id": case.id,
                    "fixture_category": case.category,
                },
                ensure_ascii=False,
            ),
        },
        files={"file": (case.path.name, case.path.read_bytes(), case.mime_type)},
    )
    response.raise_for_status()
    accepted = response.json()
    operation = _wait_operation(
        client,
        knowledge_base_id,
        str(accepted["operation_id"]),
        timeout_seconds,
    )
    operation["accepted"] = accepted
    return operation, round((time.perf_counter() - started_at) * 1000, 2)


def _search(
    client: httpx.Client,
    knowledge_base_id: str,
    case: FormatCase,
    *,
    query: str,
    mode: str,
) -> tuple[list[dict[str, Any]], float]:
    started_at = time.perf_counter()
    response = client.post(
        "/knowledge/v1/search",
        headers=_headers(RUNTIME_TOKEN),
        json={
            "query": query,
            "knowledge_base_ids": [knowledge_base_id],
            "filters": {"type": "eq", "key": "fixture_id", "value": case.id},
            "mode": mode,
            "limit": 10,
        },
    )
    response.raise_for_status()
    return list(response.json()["hits"]), round((time.perf_counter() - started_at) * 1000, 2)


def _validate_searches(client: httpx.Client, knowledge_base_id: str, case: FormatCase, result: FormatResult) -> None:
    bm25_hits, bm25_latency_ms = _search(client, knowledge_base_id, case, query=case.marker, mode="bm25")
    result.search_latency_ms["bm25"] = bm25_latency_ms
    same_fixture = all(hit.get("attributes", {}).get("fixture_id") == case.id for hit in bm25_hits)
    bm25_passed = (
        bool(bm25_hits) and same_fixture and any(case.marker in str(hit.get("content", "")) for hit in bm25_hits)
    )
    result.mode_passed["bm25"] = bm25_passed
    if not bm25_passed:
        raise AssertionError(f"{case.id} 的 bm25 检索未命中预期文件或唯一标记")

    for mode in ("dense", "hybrid"):
        # 测试 Stub 只是文本哈希：Dense 必须用已索引的原 Chunk 才有确定性；
        # Hybrid 仍用唯一标记，验证 BM25 候选和融合链路没有被 Dense 分支破坏。
        query = str(bm25_hits[0]["content"]) if mode == "dense" else case.marker
        hits, latency_ms = _search(client, knowledge_base_id, case, query=query, mode=mode)
        result.search_latency_ms[mode] = latency_ms
        same_fixture = all(hit.get("attributes", {}).get("fixture_id") == case.id for hit in hits)
        passed = bool(hits) and same_fixture
        result.mode_passed[mode] = passed
        if not passed:
            raise AssertionError(f"{case.id} 的 {mode} 检索未命中预期文件或唯一标记")

    answer_hits, latency_ms = _search(
        client,
        knowledge_base_id,
        case,
        query=case.expected_answer,
        mode="bm25",
    )
    result.search_latency_ms["answer_probe"] = latency_ms
    result.answer_probe_passed = any(case.expected_answer in str(hit.get("content", "")) for hit in answer_hits)
    if not result.answer_probe_passed:
        raise AssertionError(f"{case.id} 的答案探针未检索到：{case.expected_answer}")


def _resource_summary(samples: list[ResourceSample]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[ResourceSample]] = {}
    for sample in samples:
        grouped.setdefault((sample.phase, sample.service), []).append(sample)
    rows: list[dict[str, Any]] = []
    for (phase, service), group in sorted(grouped.items()):
        cpu_values = [sample.cpu_percent for sample in group]
        memory_values = [sample.memory_bytes / 1024**2 for sample in group]
        rows.append(
            {
                "phase": phase,
                "service": service,
                "samples": len(group),
                "cpu_percent_mean": round(statistics.mean(cpu_values), 2),
                "cpu_percent_max": round(max(cpu_values), 2),
                "memory_mib_mean": round(statistics.mean(memory_values), 2),
                "memory_mib_max": round(max(memory_values), 2),
            }
        )
    return rows


def _cleanup(client: httpx.Client, knowledge_base_id: str | None, results: list[FormatResult]) -> list[str]:
    if knowledge_base_id is None:
        return []
    errors: list[str] = []
    for result in results:
        if result.file_id is None:
            continue
        response = client.delete(
            f"/knowledge/v1/knowledge-bases/{knowledge_base_id}/files/{result.file_id}",
            headers=_headers(RUNTIME_TOKEN),
        )
        if response.status_code not in {204, 404}:
            errors.append(f"delete file {result.file_id}: HTTP {response.status_code}")
    response = client.delete(
        f"/knowledge/v1/knowledge-bases/{knowledge_base_id}",
        headers=_headers(RUNTIME_TOKEN),
    )
    if response.status_code not in {204, 404}:
        errors.append(f"delete knowledge base {knowledge_base_id}: HTTP {response.status_code}")
    return errors


def _write_reports(
    report_dir: Path,
    *,
    run_id: str,
    tenant_id: str,
    cases: list[FormatCase],
    results: list[FormatResult],
    samples: list[ResourceSample],
    cleanup_errors: list[str],
    base_url: str,
) -> tuple[Path, Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    stem = report_dir / f"document-formats-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    resources = _resource_summary(samples)
    payload = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "tenant_id": tenant_id,
        "environment": {
            "platform": platform.platform(),
            "base_url": base_url,
            "embedding": "deterministic test stub; 仅验证链路，不评价语义效果",
            "ocr": False,
        },
        "cases": [
            {
                **asdict(result),
                "marker": case.marker,
                "question": case.question,
                "expected_answer": case.expected_answer,
                "features": list(case.features),
            }
            for case, result in zip(cases, results, strict=True)
        ],
        "resource_summary": resources,
        "resource_samples": [asdict(sample) for sample in samples],
        "cleanup_errors": cleanup_errors,
    }
    json_path = stem.with_suffix(".json")
    csv_path = stem.with_suffix(".resources.csv")
    markdown_path = stem.with_suffix(".md")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with csv_path.open("w", encoding="utf-8", newline="") as output:
        fieldnames = list(asdict(samples[0])) if samples else []
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        if samples:
            writer.writeheader()
            writer.writerows(asdict(sample) for sample in samples)

    result_rows = "\n".join(
        (
            "| {category} | {filename} | {status} | {operation} | {bm25} | {dense} | "
            "{hybrid} | {answer} | {latency} | {error} |"
        ).format(
            category=result.category,
            filename=result.filename,
            status=result.status,
            operation=result.operation_status or "-",
            bm25="通过" if result.mode_passed.get("bm25") else "未通过",
            dense="通过" if result.mode_passed.get("dense") else "未通过",
            hybrid="通过" if result.mode_passed.get("hybrid") else "未通过",
            answer="通过" if result.answer_probe_passed else "未通过",
            latency=f"{result.ingest_latency_ms:.2f}" if result.ingest_latency_ms is not None else "-",
            error=(result.error or "-").replace("|", "\\|").replace("\n", " "),
        )
        for result in results
    )
    resource_rows = "\n".join(
        f"| {row['phase']} | {row['service']} | {row['samples']} | {row['cpu_percent_mean']:.2f}% | "
        f"{row['cpu_percent_max']:.2f}% | {row['memory_mib_mean']:.2f} | {row['memory_mib_max']:.2f} |"
        for row in resources
    )
    markdown_path.write_text(
        f"""# 首批非 OCR 文档格式验证

## 判定边界

- 共七类、八个文件；Markdown/TXT 为同一类的两个输入变体。
- 每个文件都执行真实异步导入，并以唯一标记验证 BM25，以同文件过滤验证 Dense/Hybrid 协议。
- 答案探针要求检索结果正文包含预先标注的答案。
- Embedding 使用确定性测试桩，因此本报告不评价语义召回质量。
- OCR 关闭；图片和扫描 PDF 不在本轮范围。

## 格式结果

| 类别 | 文件 | 总结果 | 异步任务 | BM25 | Dense | Hybrid | 答案探针 | 导入 ms | 错误 |
| --- | --- | --- | --- | --- | --- | --- | --- | ---: | --- |
{result_rows}

## 容器资源

CPU 百分比沿用 Docker 定义，100% 约等于一个逻辑 CPU 核；不包含远程模型服务成本。

| 阶段 | 服务 | 样本数 | CPU 平均 | CPU 最大 | 内存平均 MiB | 内存最大 MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
{resource_rows}

## 清理

{("- 无清理错误。" if not cleanup_errors else "- " + "\n- ".join(cleanup_errors))}
""",
        encoding="utf-8",
    )
    return json_path, csv_path, markdown_path


def _build_parser() -> argparse.ArgumentParser:
    repository_root = Path(__file__).parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8321")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=repository_root / "tests" / "fixtures" / "document_formats" / "manifest.json",
    )
    parser.add_argument("--operation-timeout", type=float, default=300)
    parser.add_argument("--baseline-seconds", type=float, default=2.0)
    parser.add_argument("--report-dir", type=Path, default=repository_root / ".reports" / "document-formats")
    parser.add_argument("--keep", action="store_true", help="保留本次创建的技术知识库与文件")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if args.operation_timeout < 1 or args.baseline_seconds < 1:
        raise SystemExit("operation-timeout 和 baseline-seconds 必须至少为 1")
    cases = _load_cases(args.manifest.resolve())
    repository_root = Path(__file__).parents[2]
    run_id = uuid.uuid4().hex[:10]
    tenant_id = f"format-validation-{run_id}"
    sampler = DockerStatsSampler(repository_root)
    results = [FormatResult(id=case.id, category=case.category, filename=case.path.name) for case in cases]
    knowledge_base_id: str | None = None
    cleanup_errors: list[str] = []

    with httpx.Client(base_url=args.base_url, timeout=max(30, args.operation_timeout), trust_env=False) as client:
        try:
            _configure_embedding(client, tenant_id)
            knowledge_base_id = _create_knowledge_base(client, tenant_id, run_id)
            sampler.start()
            sampler.set_phase("baseline")
            time.sleep(args.baseline_seconds)

            for case, result in zip(cases, results, strict=True):
                sampler.set_phase(f"ingest_{case.id}")
                try:
                    operation, latency_ms = _ingest_case(
                        client,
                        knowledge_base_id,
                        case,
                        run_id,
                        args.operation_timeout,
                    )
                    accepted = operation["accepted"]
                    result.operation_id = str(accepted["operation_id"])
                    result.file_id = str(accepted["file_id"])
                    result.operation_status = str(operation["status"])
                    result.ingest_latency_ms = latency_ms
                    if operation["status"] != "completed":
                        last_error = operation.get("last_error") or {}
                        raise RuntimeError(
                            f"异步导入状态为 {operation['status']}："
                            f"{last_error.get('code', 'unknown')} {last_error.get('message', '')}".strip()
                        )
                    sampler.set_phase(f"search_{case.id}")
                    _validate_searches(client, knowledge_base_id, case, result)
                    result.status = "passed"
                except Exception as exc:  # noqa: BLE001 - 需要保留完整格式矩阵，而不是首错退出。
                    result.status = "failed"
                    result.error = f"{type(exc).__name__}: {exc}"
        finally:
            sampler.set_phase("cleanup")
            if not args.keep:
                cleanup_errors = _cleanup(client, knowledge_base_id, results)
            time.sleep(1.2)
            sampler.stop()

    paths = _write_reports(
        args.report_dir,
        run_id=run_id,
        tenant_id=tenant_id,
        cases=cases,
        results=results,
        samples=sampler.samples,
        cleanup_errors=cleanup_errors,
        base_url=args.base_url,
    )
    failed = [result.id for result in results if result.status != "passed"]
    print(
        json.dumps(
            {
                "status": "passed" if not failed else "completed_with_failures",
                "passed": [result.id for result in results if result.status == "passed"],
                "failed": failed,
                "reports": list(map(str, paths)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
