"""模拟 Stella 与 Cherry Studio 企业版的实际知识库调用，并采集容器资源。

该脚本验证产品到统一知识库的对象映射、可见范围和挂载语义。默认数据量只用于
功能验收与初步资源画像，不代表容量压测或生产资源承诺。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import platform
import statistics
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from shared_knowledge_service.provider.config import tenant_collection_name

SEARCH_QUERY = "统一知识库权限挂载校验"
SEARCH_MODES = ("bm25", "dense", "hybrid")
PRODUCTION_SERVICES = ("knowledge-ogx", "postgres", "qdrant")
TEST_SERVICES = (*PRODUCTION_SERVICES, "embedding-stub")


@dataclass(frozen=True, slots=True)
class SearchCase:
    """一条产品检索场景及其期望可见文档。"""

    product: str
    name: str
    knowledge_base_ids: tuple[str, ...]
    filters: dict[str, Any] | None
    expected_document_keys: frozenset[str]


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    """一次场景验证结果。"""

    product: str
    scenario: str
    operation: str
    mode: str | None
    status: str
    latency_ms: float
    expected_documents: list[str]
    actual_documents: list[str]


@dataclass(frozen=True, slots=True)
class ResourceSample:
    """Docker 在一个时间点返回的容器资源样本。"""

    elapsed_seconds: float
    timestamp: str
    phase: str
    service: str
    container: str
    cpu_percent: float
    memory_bytes: int
    memory_limit_bytes: int
    pids: int


def _parse_size(value: str) -> int:
    """把 Docker Stats 的 IEC/SI 容量字符串转换为字节。"""

    normalized = value.strip()
    units = {
        "B": 1,
        "kB": 1000,
        "KB": 1000,
        "KiB": 1024,
        "MB": 1000**2,
        "MiB": 1024**2,
        "GB": 1000**3,
        "GiB": 1024**3,
    }
    for unit in sorted(units, key=len, reverse=True):
        if normalized.endswith(unit):
            return int(float(normalized[: -len(unit)]) * units[unit])
    raise ValueError(f"无法解析 Docker 容量：{value}")


def _percentile(values: list[float], percentile: float) -> float:
    """使用 nearest-rank 计算小样本百分位。"""

    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


class DockerStatsSampler:
    """持续读取 Docker Stats，并给每条样本附上当前业务阶段。"""

    def __init__(self, repository_root: Path) -> None:
        self.repository_root = repository_root
        self.samples: list[ResourceSample] = []
        self._phase = "initializing"
        self._phase_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._started_at = 0.0
        self._service_by_container = self._discover_containers()

    def _compose_command(self, *arguments: str) -> list[str]:
        return ["docker", "compose", "-f", "compose.yaml", "-f", "compose.e2e.yaml", *arguments]

    def _discover_containers(self) -> dict[str, str]:
        result = subprocess.run(
            self._compose_command("ps", "--format", "json"),
            cwd=self.repository_root,
            check=True,
            capture_output=True,
            text=True,
        )
        discovered: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            service = str(row["Service"])
            if service in TEST_SERVICES:
                discovered[str(row["Name"])] = service
        missing = sorted(set(PRODUCTION_SERVICES) - set(discovered.values()))
        if missing:
            raise RuntimeError(f"以下服务尚未运行：{', '.join(missing)}")
        return discovered

    def set_phase(self, phase: str) -> None:
        """切换后续资源样本所属阶段。"""

        with self._phase_lock:
            self._phase = phase

    def start(self) -> None:
        """启动 Docker Stats 流读取线程。"""

        if self._process is not None:
            raise RuntimeError("资源采样器不能重复启动")
        self._started_at = time.perf_counter()
        self._process = subprocess.Popen(
            ["docker", "stats", "--format", "{{json .}}", *self._service_by_container],
            cwd=self.repository_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._reader = threading.Thread(target=self._read_samples, name="docker-stats-reader", daemon=True)
        self._reader.start()

    def _read_samples(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for raw_line in process.stdout:
            # Docker Stats 的连续模式可能带终端刷新控制符，只截取完整 JSON 对象。
            start = raw_line.find("{")
            end = raw_line.rfind("}")
            if start < 0 or end <= start:
                continue
            try:
                row = json.loads(raw_line[start : end + 1])
                current_memory, memory_limit = str(row["MemUsage"]).split("/", 1)
                with self._phase_lock:
                    phase = self._phase
                container = str(row.get("Name") or row["Container"])
                service = self._service_by_container.get(container)
                if service is None:
                    continue
                self.samples.append(
                    ResourceSample(
                        elapsed_seconds=round(time.perf_counter() - self._started_at, 3),
                        timestamp=datetime.now(UTC).isoformat(),
                        phase=phase,
                        service=service,
                        container=container,
                        cpu_percent=float(str(row["CPUPerc"]).rstrip("%")),
                        memory_bytes=_parse_size(current_memory),
                        memory_limit_bytes=_parse_size(memory_limit),
                        pids=int(row["PIDs"]),
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                # 单条采样格式异常不应中断产品场景；最终报告会保留实际样本数。
                continue

    def stop(self) -> None:
        """停止采样，并等待读取线程退出。"""

        process = self._process
        if process is None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if self._reader is not None:
            self._reader.join(timeout=5)
        self._process = None


class ProductSimulation:
    """建立两侧对象、执行检索矩阵并负责清理。"""

    def __init__(self, base_url: str, qdrant_url: str, collection_prefix: str) -> None:
        self.run_id = uuid.uuid4().hex[:10]
        self.client = httpx.Client(
            base_url=base_url,
            timeout=300,
            trust_env=False,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        )
        self.qdrant_client = httpx.Client(base_url=qdrant_url, timeout=30, trust_env=False)
        self.collection_prefix = collection_prefix
        self.tenant_ids: set[str] = set()
        self.knowledge_base_ids: list[str] = []
        self.file_ids: list[str] = []
        self.results: list[ScenarioResult] = []

    def _create_knowledge_base(self, product: str, business_key: str, tenant_id: str) -> str:
        started_at = time.perf_counter()
        response = self.client.post(
            "/v1/vector_stores",
            json={
                "name": f"simulation-{product}-{business_key}-{self.run_id}",
                "metadata": {
                    "simulation_run": self.run_id,
                    "product": product,
                    "business_key": business_key,
                    "tenant_id": tenant_id,
                },
            },
        )
        response.raise_for_status()
        knowledge_base_id = str(response.json()["id"])
        self.tenant_ids.add(tenant_id)
        self.knowledge_base_ids.append(knowledge_base_id)

        # 企业版和 Stella 都需要持久化该映射，因此创建后立即验证可读取。
        read_response = self.client.get(f"/v1/vector_stores/{knowledge_base_id}")
        read_response.raise_for_status()
        self.results.append(
            ScenarioResult(
                product=product,
                scenario=business_key,
                operation="create_knowledge_base",
                mode=None,
                status="passed",
                latency_ms=round((time.perf_counter() - started_at) * 1000, 2),
                expected_documents=[],
                actual_documents=[],
            )
        )
        return knowledge_base_id

    def _ingest_document(
        self,
        *,
        product: str,
        scenario: str,
        knowledge_base_id: str,
        document_key: str,
        attributes: dict[str, str],
    ) -> str:
        content = (
            f"# {document_key}\n\n"
            f"{SEARCH_QUERY}。\n\n"
            f"本段只用于验证文档 {document_key} 在正确的产品范围内可见，其他范围不得返回。\n"
        ).encode()
        complete_attributes = {
            "simulation_run": self.run_id,
            "product": product,
            "document_key": document_key,
            **attributes,
        }
        started_at = time.perf_counter()
        response = self.client.post(
            "/knowledge/v1/ingest",
            files={"file": (f"{document_key.replace(':', '-')}.md", content, "text/markdown")},
            data={
                "knowledge_base_id": knowledge_base_id,
                "attributes": json.dumps(complete_attributes, ensure_ascii=False),
            },
        )
        response.raise_for_status()
        body = response.json()
        if response.status_code != 202 or body.get("status") != "processing":
            raise RuntimeError(f"异步导入 {document_key} 未被可靠接收：{body}")

        operation_id = str(body["operation_id"])
        file_id = str(body["file_id"])
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            operation = self.client.get(
                f"/knowledge/v1/knowledge-bases/{knowledge_base_id}/operations/{operation_id}",
            )
            operation.raise_for_status()
            operation_body = operation.json()
            if operation_body["status"] == "completed":
                break
            if operation_body["status"] in {"failed", "cancelled"}:
                raise RuntimeError(f"异步导入 {document_key} 失败：{operation_body.get('last_error')}")
            time.sleep(0.1)
        else:
            raise TimeoutError(f"异步导入 {document_key} 在 300 秒内未完成")

        self.file_ids.append(file_id)
        self.results.append(
            ScenarioResult(
                product=product,
                scenario=scenario,
                operation="ingest",
                mode=None,
                status="passed",
                latency_ms=round((time.perf_counter() - started_at) * 1000, 2),
                expected_documents=[document_key],
                actual_documents=[document_key],
            )
        )
        return file_id

    def prepare_stella(self) -> list[SearchCase]:
        """创建一个隐藏知识库，并写入 2×2 身份矩阵所需的九份文档。"""

        tenant_id = f"stella-{self.run_id}"
        hidden_id = self._create_knowledge_base("stella", "hidden-library", tenant_id)
        self._ingest_document(
            product="stella",
            scenario="system",
            knowledge_base_id=hidden_id,
            document_key="stella:system",
            attributes={"scope": "system"},
        )
        for agent_id in ("agent-a", "agent-b"):
            self._ingest_document(
                product="stella",
                scenario="system_agent",
                knowledge_base_id=hidden_id,
                document_key=f"stella:system_agent:{agent_id}",
                attributes={"scope": "system_agent", "agent_id": agent_id},
            )
        for user_id in ("user-1", "user-2"):
            self._ingest_document(
                product="stella",
                scenario="user",
                knowledge_base_id=hidden_id,
                document_key=f"stella:user:{user_id}",
                attributes={"scope": "user", "user_id": user_id},
            )
            for agent_id in ("agent-a", "agent-b"):
                self._ingest_document(
                    product="stella",
                    scenario="user_agent",
                    knowledge_base_id=hidden_id,
                    document_key=f"stella:user_agent:{user_id}:{agent_id}",
                    attributes={"scope": "user_agent", "user_id": user_id, "agent_id": agent_id},
                )

        cases: list[SearchCase] = []
        for user_id in ("user-1", "user-2"):
            for agent_id in ("agent-a", "agent-b"):
                filters = {
                    "type": "or",
                    "filters": [
                        {"type": "eq", "key": "scope", "value": "system"},
                        {
                            "type": "and",
                            "filters": [
                                {"type": "eq", "key": "scope", "value": "system_agent"},
                                {"type": "eq", "key": "agent_id", "value": agent_id},
                            ],
                        },
                        {
                            "type": "and",
                            "filters": [
                                {"type": "eq", "key": "scope", "value": "user"},
                                {"type": "eq", "key": "user_id", "value": user_id},
                            ],
                        },
                        {
                            "type": "and",
                            "filters": [
                                {"type": "eq", "key": "scope", "value": "user_agent"},
                                {"type": "eq", "key": "user_id", "value": user_id},
                                {"type": "eq", "key": "agent_id", "value": agent_id},
                            ],
                        },
                    ],
                }
                expected = frozenset(
                    {
                        "stella:system",
                        f"stella:system_agent:{agent_id}",
                        f"stella:user:{user_id}",
                        f"stella:user_agent:{user_id}:{agent_id}",
                    }
                )
                cases.append(
                    SearchCase(
                        product="stella",
                        name=f"{user_id}+{agent_id}",
                        knowledge_base_ids=(hidden_id,),
                        filters=filters,
                        expected_document_keys=expected,
                    )
                )
        return cases

    def prepare_enterprise(self) -> tuple[list[SearchCase], dict[str, dict[str, str]]]:
        """为两个租户创建公司/产品知识库，并返回 Assistant 挂载组合。"""

        mounts = {
            "company-only": ("company",),
            "product-a-only": ("product-a",),
            "company+product-a": ("company", "product-a"),
            "company+product-b": ("company", "product-b"),
            "all-mounted": ("company", "product-a", "product-b"),
        }
        cases: list[SearchCase] = []
        stores_by_tenant: dict[str, dict[str, str]] = {}
        for tenant_name in ("tenant-a", "tenant-b"):
            tenant_id = f"enterprise-{tenant_name}-{self.run_id}"
            knowledge_bases: dict[str, str] = {}
            documents: dict[str, str] = {}
            for business_key in ("company", "product-a", "product-b"):
                knowledge_base_id = self._create_knowledge_base(
                    "enterprise",
                    f"{tenant_name}:{business_key}",
                    tenant_id,
                )
                document_key = f"enterprise:{tenant_name}:{business_key}"
                self._ingest_document(
                    product="enterprise",
                    scenario=f"{tenant_name}:{business_key}",
                    knowledge_base_id=knowledge_base_id,
                    document_key=document_key,
                    attributes={"business_key": business_key},
                )
                knowledge_bases[business_key] = knowledge_base_id
                documents[business_key] = document_key
            stores_by_tenant[tenant_name] = knowledge_bases
            cases.extend(
                SearchCase(
                    product="enterprise",
                    name=f"{tenant_name}:{name}",
                    knowledge_base_ids=tuple(knowledge_bases[key] for key in keys),
                    filters=None,
                    expected_document_keys=frozenset(documents[key] for key in keys),
                )
                for name, keys in mounts.items()
            )

        # 无挂载时企业版不应向统一知识库发送空 knowledge_base_ids 请求。
        self.results.append(
            ScenarioResult(
                product="enterprise",
                scenario="no-mounted-knowledge-base",
                operation="product_skips_search",
                mode=None,
                status="passed",
                latency_ms=0.0,
                expected_documents=[],
                actual_documents=[],
            )
        )
        return cases, stores_by_tenant

    def verify_tenant_collections(self) -> None:
        """验证每个租户恰好映射到一个独立 Qdrant Collection。"""

        expected = {tenant_collection_name(self.collection_prefix, tenant_id) for tenant_id in self.tenant_ids}
        response = self.qdrant_client.get("/collections")
        response.raise_for_status()
        actual = {str(item["name"]) for item in response.json()["result"]["collections"]}
        missing = expected - actual
        if missing:
            raise AssertionError(f"租户 Collection 未创建：{sorted(missing)}")
        self.results.append(
            ScenarioResult(
                product="infrastructure",
                scenario="tenant-collection-routing",
                operation="verify_collection_isolation",
                mode=None,
                status="passed",
                latency_ms=0.0,
                expected_documents=sorted(expected),
                actual_documents=sorted(expected & actual),
            )
        )

    def verify_cross_tenant_search_is_rejected(self, stores_by_tenant: dict[str, dict[str, str]]) -> None:
        """验证一次 Search 不能把两个租户的逻辑知识库混在一起。"""

        started_at = time.perf_counter()
        response = self.client.post(
            "/knowledge/v1/search",
            json={
                "query": SEARCH_QUERY,
                "knowledge_base_ids": [
                    stores_by_tenant["tenant-a"]["company"],
                    stores_by_tenant["tenant-b"]["company"],
                ],
                "mode": "hybrid",
                "limit": 10,
            },
        )
        if response.status_code != 422 or "不能跨租户 Collection" not in response.text:
            raise AssertionError(f"跨租户 Search 应返回 422：status={response.status_code} body={response.text[:300]}")
        self.results.append(
            ScenarioResult(
                product="enterprise",
                scenario="cross-tenant-mounted-knowledge-bases",
                operation="search_rejected",
                mode="hybrid",
                status="passed",
                latency_ms=round((time.perf_counter() - started_at) * 1000, 2),
                expected_documents=[],
                actual_documents=[],
            )
        )

    def _search_hits(self, case: SearchCase, mode: str, query: str) -> tuple[list[dict[str, Any]], float]:
        started_at = time.perf_counter()
        response = self.client.post(
            "/knowledge/v1/search",
            json={
                "query": query,
                "knowledge_base_ids": list(case.knowledge_base_ids),
                "filters": case.filters,
                "mode": mode,
                "limit": 10,
            },
        )
        response.raise_for_status()
        return list(response.json()["hits"]), round((time.perf_counter() - started_at) * 1000, 2)

    def execute_search_case(self, case: SearchCase, mode: str, *, record: bool = True) -> float:
        """验证挂载/权限边界；Dense 使用精确原文规避测试 Stub 的随机语义。"""

        dense_target: str | None = None
        query = SEARCH_QUERY
        if mode == "dense":
            bootstrap_hits, _ = self._search_hits(case, "bm25", SEARCH_QUERY)
            if not bootstrap_hits:
                raise AssertionError(f"{case.product}/{case.name} 没有可用于 Dense 探针的 BM25 命中")
            dense_target = str(bootstrap_hits[0]["attributes"]["document_key"])
            query = str(bootstrap_hits[0]["content"])

        hits, latency_ms = self._search_hits(case, mode, query)
        actual = {str(hit["attributes"]["document_key"]) for hit in hits if "document_key" in hit.get("attributes", {})}
        expected = set(case.expected_document_keys)
        if mode == "dense":
            correct = bool(actual) and actual <= expected and dense_target in actual
        else:
            correct = actual == expected
        if not correct:
            raise AssertionError(
                f"{case.product}/{case.name}/{mode} 可见范围错误："
                f"expected={sorted(case.expected_document_keys)} actual={sorted(actual)}"
            )
        if record:
            self.results.append(
                ScenarioResult(
                    product=case.product,
                    scenario=case.name,
                    operation="search",
                    mode=mode,
                    status="passed",
                    latency_ms=latency_ms,
                    expected_documents=sorted(case.expected_document_keys),
                    actual_documents=sorted(actual),
                )
            )
        return latency_ms

    def execute_correctness_matrix(self, cases: list[SearchCase]) -> None:
        for case in cases:
            for mode in SEARCH_MODES:
                self.execute_search_case(case, mode)

    def execute_concurrent_search(
        self,
        cases: list[SearchCase],
        *,
        rounds: int,
        concurrency: int,
    ) -> dict[str, Any]:
        """混合两侧典型 Hybrid 请求，生成足以采样的短时并发负载。"""

        scheduled = [case for _ in range(rounds) for case in cases]
        latencies: list[float] = []
        started_at = time.perf_counter()
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="knowledge-search") as executor:
            futures = [executor.submit(self.execute_search_case, case, "hybrid", record=False) for case in scheduled]
            for future in as_completed(futures):
                latencies.append(future.result())
        elapsed = time.perf_counter() - started_at
        return {
            "request_count": len(latencies),
            "concurrency": concurrency,
            "elapsed_seconds": round(elapsed, 3),
            "throughput_rps": round(len(latencies) / elapsed, 2),
            "latency_ms_mean": round(statistics.mean(latencies), 2),
            "latency_ms_p50": round(statistics.median(latencies), 2),
            "latency_ms_p95": round(_percentile(latencies, 0.95), 2),
            "latency_ms_max": round(max(latencies), 2),
        }

    def cleanup(self) -> list[str]:
        """清理本次 VectorStore、原文件和已空的测试租户 Collection。"""

        errors: list[str] = []
        for knowledge_base_id in self.knowledge_base_ids:
            response = self.client.delete(f"/v1/vector_stores/{knowledge_base_id}")
            if response.status_code not in {200, 404}:
                errors.append(f"delete vector store {knowledge_base_id}: HTTP {response.status_code}")
        for file_id in self.file_ids:
            response = self.client.delete(f"/v1/files/{file_id}")
            if response.status_code not in {200, 404}:
                errors.append(f"delete file {file_id}: HTTP {response.status_code}")
        for tenant_id in self.tenant_ids:
            collection_name = tenant_collection_name(self.collection_prefix, tenant_id)
            count_response = self.qdrant_client.post(
                f"/collections/{collection_name}/points/count",
                json={"exact": True},
            )
            if count_response.status_code == 404:
                continue
            if count_response.status_code != 200:
                errors.append(f"count collection {collection_name}: HTTP {count_response.status_code}")
                continue
            if int(count_response.json()["result"]["count"]) != 0:
                errors.append(f"collection {collection_name} 清理后仍有 Point，未删除 Collection")
                continue
            delete_response = self.qdrant_client.delete(f"/collections/{collection_name}")
            if delete_response.status_code not in {200, 404}:
                errors.append(f"delete collection {collection_name}: HTTP {delete_response.status_code}")
        return errors

    def close(self) -> None:
        self.client.close()
        self.qdrant_client.close()


def _resource_summary(samples: list[ResourceSample]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[ResourceSample]] = {}
    for sample in samples:
        grouped.setdefault((sample.phase, sample.service), []).append(sample)
    rows: list[dict[str, Any]] = []
    for (phase, service), group in sorted(grouped.items()):
        cpu = [sample.cpu_percent for sample in group]
        memory_mib = [sample.memory_bytes / 1024**2 for sample in group]
        rows.append(
            {
                "phase": phase,
                "service": service,
                "samples": len(group),
                "cpu_percent_mean": round(statistics.mean(cpu), 2),
                "cpu_percent_p95": round(_percentile(cpu, 0.95), 2),
                "cpu_percent_max": round(max(cpu), 2),
                "memory_mib_mean": round(statistics.mean(memory_mib), 2),
                "memory_mib_max": round(max(memory_mib), 2),
                "pids_max": max(sample.pids for sample in group),
            }
        )
    return rows


def _write_reports(
    report_dir: Path,
    *,
    simulation: ProductSimulation,
    sampler: DockerStatsSampler,
    load_result: dict[str, Any],
    cleanup_errors: list[str],
    args: argparse.Namespace,
) -> tuple[Path, Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = report_dir / f"product-simulation-{stamp}"
    json_path = stem.with_suffix(".json")
    csv_path = stem.with_suffix(".resources.csv")
    markdown_path = stem.with_suffix(".md")
    resources = _resource_summary(sampler.samples)
    payload = {
        "run_id": simulation.run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "environment": {
            "platform": platform.platform(),
            "logical_cpu_count": os.cpu_count(),
            "base_url": args.base_url,
            "qdrant_url": args.qdrant_url,
            "collection_prefix": args.collection_prefix,
            "embedding_service": "deterministic test stub"
            if "embedding-stub" in sampler._service_by_container.values()
            else "external",
            "rerank": "由当前部署开关决定；本次 E2E Compose 默认使用 deterministic stub",
        },
        "parameters": {
            "baseline_seconds": args.baseline_seconds,
            "search_rounds": args.search_rounds,
            "search_concurrency": args.search_concurrency,
        },
        "scenario_results": [asdict(result) for result in simulation.results],
        "load": load_result,
        "resource_summary": resources,
        "resource_sample_count": len(sampler.samples),
        "cleanup_errors": cleanup_errors,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with csv_path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=list(asdict(sampler.samples[0])) if sampler.samples else [])
        if sampler.samples:
            writer.writeheader()
            writer.writerows(asdict(sample) for sample in sampler.samples)

    scenario_rows = "\n".join(
        f"| {result.product} | {result.scenario} | {result.operation} | {result.mode or '-'} | "
        f"{result.status} | {result.latency_ms:.2f} |"
        for result in simulation.results
    )
    resource_rows = "\n".join(
        f"| {row['phase']} | {row['service']} | {row['samples']} | {row['cpu_percent_mean']:.2f}% | "
        f"{row['cpu_percent_p95']:.2f}% | {row['cpu_percent_max']:.2f}% | "
        f"{row['memory_mib_mean']:.2f} | {row['memory_mib_max']:.2f} |"
        for row in resources
    )
    markdown_path.write_text(
        f"""# Stella 与 Cherry Studio 企业版统一知识库模拟结果

## 测试边界

- Stella：一个隐藏知识库，覆盖 2 个用户 × 2 个 Agent 的四象限累加与交叉隔离。
- 企业版：两个租户各有公司、产品 A、产品 B 三个显式知识库，覆盖单挂载、多挂载、全挂载和无挂载。
- 存储路由：验证每个租户对应一个 Collection，并验证一次 Search 混入两个租户的知识库时返回 422。
- 正确性覆盖 BM25、Dense、Hybrid；并发阶段使用 Hybrid。
- Dense 使用已命中 Chunk 原文验证，并要求结果非空且全部位于允许范围内；测试 Stub 不承担语义召回评测。
- 本次使用确定性 Embedding/Rerank 测试桩，资源数据不包含真实远程模型服务成本，也不代表检索效果。
- 文件均为小型 Markdown；结果用于接口与初步资源画像，不是容量测试。

## 场景结果

| 产品 | 场景 | 操作 | 模式 | 结果 | 延迟 ms |
| --- | --- | --- | --- | --- | ---: |
{scenario_rows}

## 混合并发搜索

| 请求数 | 并发 | 总耗时 s | 吞吐 req/s | 平均 ms | P50 ms | P95 ms | 最大 ms |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| {load_result["request_count"]} | {load_result["concurrency"]} | {load_result["elapsed_seconds"]} | """
        f"{load_result['throughput_rps']} | {load_result['latency_ms_mean']} | {load_result['latency_ms_p50']} | "
        f"{load_result['latency_ms_p95']} | {load_result['latency_ms_max']} |\n\n"
        f"""## 容器资源

CPU 百分比沿用 Docker 定义，100% 约等于一个逻辑 CPU 核。内存是容器当前工作集，不是远程模型服务占用。

| 阶段 | 服务 | 样本数 | CPU 平均 | CPU P95 | CPU 最大 | 内存平均 MiB | 内存最大 MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
{resource_rows}

## 清理

{("- 无清理错误。" if not cleanup_errors else "- " + "\n- ".join(cleanup_errors))}
""",
        encoding="utf-8",
    )
    return json_path, csv_path, markdown_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8321")
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    parser.add_argument("--collection-prefix", default="shared_knowledge")
    parser.add_argument("--baseline-seconds", type=float, default=4.0)
    parser.add_argument("--search-rounds", type=int, default=50)
    parser.add_argument("--search-concurrency", type=int, default=8)
    parser.add_argument("--report-dir", type=Path, default=Path(".reports"))
    parser.add_argument("--keep", action="store_true", help="保留本次创建的知识库与文件")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if args.baseline_seconds < 1:
        raise SystemExit("baseline-seconds 必须至少为 1")
    if args.search_rounds < 1 or args.search_concurrency < 1:
        raise SystemExit("search-rounds 和 search-concurrency 必须为正数")

    repository_root = Path(__file__).parents[2]
    sampler = DockerStatsSampler(repository_root)
    simulation = ProductSimulation(args.base_url, args.qdrant_url, args.collection_prefix)
    cleanup_errors: list[str] = []
    load_result: dict[str, Any] = {}
    try:
        sampler.start()
        sampler.set_phase("baseline")
        time.sleep(args.baseline_seconds)

        sampler.set_phase("stella_setup_and_ingest")
        stella_cases = simulation.prepare_stella()
        sampler.set_phase("stella_search_matrix")
        simulation.execute_correctness_matrix(stella_cases)

        sampler.set_phase("enterprise_setup_and_ingest")
        enterprise_cases, stores_by_tenant = simulation.prepare_enterprise()
        simulation.verify_tenant_collections()
        simulation.verify_cross_tenant_search_is_rejected(stores_by_tenant)
        sampler.set_phase("enterprise_search_matrix")
        simulation.execute_correctness_matrix(enterprise_cases)

        sampler.set_phase("mixed_concurrent_hybrid_search")
        load_result = simulation.execute_concurrent_search(
            [*stella_cases, *enterprise_cases],
            rounds=args.search_rounds,
            concurrency=args.search_concurrency,
        )
    finally:
        sampler.set_phase("cleanup")
        if not args.keep:
            cleanup_errors = simulation.cleanup()
        # 给持续采样流一个机会记录清理后的稳定状态。
        time.sleep(1.2)
        sampler.stop()
        simulation.close()

    json_path, csv_path, markdown_path = _write_reports(
        args.report_dir,
        simulation=simulation,
        sampler=sampler,
        load_result=load_result,
        cleanup_errors=cleanup_errors,
        args=args,
    )
    print(
        json.dumps(
            {
                "status": "passed",
                "scenario_count": len(simulation.results),
                "load": load_result,
                "resource_samples": len(sampler.samples),
                "reports": [str(json_path), str(csv_path), str(markdown_path)],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
