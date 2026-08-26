"""供 E2E 使用的确定性 Embedding 与 Rerank 模型服务 Stub。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def deterministic_embedding(text: str, dimension: int = 3) -> list[float]:
    """把文本稳定映射为单位向量；只验证链路，不代表真实语义效果。"""

    digest = hashlib.sha256(text.encode("utf-8")).digest()
    values = [digest[index] / 127.5 - 1.0 for index in range(dimension)]
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


class EmbeddingHandler(BaseHTTPRequestHandler):
    """实现测试所需的最小 OpenAI-compatible Embedding 与 Rerank 协议。"""

    server_version = "KnowledgeEmbeddingStub/1.0"
    failure_file: Path | None = None
    # 只有带 keyed- 前缀的测试模型校验固定 Key，用于证明 KB 凭证不会串用。
    keyed_models = {
        "keyed-embedding-a": "embedding-key-a",
        "keyed-embedding-b": "embedding-key-b",
        "keyed-rerank-a": "rerank-key-a",
        "keyed-rerank-b": "rerank-key-b",
    }

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._write_json(HTTPStatus.OK, {"status": "ok"})
            return
        if self.path == "/v1/models":
            # OGX 启动时会刷新 Provider 的模型列表；测试桩显式返回两类模型，避免产生误导性的 404 日志。
            self._write_json(
                HTTPStatus.OK,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "deterministic-test",
                            "object": "model",
                            "created": 0,
                            "owned_by": "test-stub",
                        }
                    ],
                },
            )
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/v1/embeddings":
            self._handle_embeddings()
            return
        if self.path == "/v1/rerank":
            self._handle_rerank()
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def _handle_embeddings(self) -> None:
        """返回稳定的单位向量，并保留原有故障注入入口。"""

        if self.failure_file is not None and self.failure_file.exists():
            # 故障文件只供显式恢复 E2E 注入稳定的上游 503，不进入生产镜像启动命令。
            self._write_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "injected_failure"})
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(content_length))
            raw_input = payload["input"]
            texts = [raw_input] if isinstance(raw_input, str) else list(raw_input)
            if not all(isinstance(text, str) for text in texts):
                raise ValueError("input 必须是字符串或字符串列表")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        model = str(payload.get("model", "deterministic-test"))
        if not self._authorize_keyed_model(model):
            return
        raw_dimension = payload.get("dimensions", 3)
        if not isinstance(raw_dimension, int) or raw_dimension < 1 or raw_dimension > 128:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": "dimensions 必须为 1～128 的整数"})
            return
        self._write_json(
            HTTPStatus.OK,
            {
                "object": "list",
                "data": [
                    {
                        "object": "embedding",
                        "index": index,
                        "embedding": deterministic_embedding(text, raw_dimension),
                    }
                    for index, text in enumerate(texts)
                ],
                "model": model,
                "usage": {"prompt_tokens": sum(len(text) for text in texts), "total_tokens": sum(map(len, texts))},
            },
        )

    def _handle_rerank(self) -> None:
        """按输入逆序返回显著测试分数，证明统一 Search 确实执行了重排。"""

        content_length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(content_length))
            query = payload["query"]
            documents = list(payload["documents"])
            top_n = int(payload.get("top_n", len(documents)))
            if not isinstance(query, str) or not all(isinstance(document, str) for document in documents):
                raise ValueError("query 和 documents 必须是字符串")
            if top_n < 1:
                raise ValueError("top_n 必须大于零")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        model = str(payload.get("model", "deterministic-rerank"))
        if not self._authorize_keyed_model(model):
            return

        ranked_indexes = list(reversed(range(len(documents))))[:top_n]
        self._write_json(
            HTTPStatus.OK,
            {
                "results": [
                    {"index": index, "relevance_score": 1000.0 - rank} for rank, index in enumerate(ranked_indexes)
                ],
                "usage": {"prompt_tokens": len(query) + sum(map(len, documents))},
            },
        )

    def _authorize_keyed_model(self, model: str) -> bool:
        """对少量 E2E 模型校验 Bearer Key，其他模型保持原有宽松行为。"""

        expected = self.keyed_models.get(model)
        if expected is None:
            return True
        if self.headers.get("Authorization") == f"Bearer {expected}":
            return True
        self._write_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_api_key"})
        return False

    def log_message(self, format_: str, *args: Any) -> None:
        # 测试输出只保留显式断言结果，避免并发请求日志干扰诊断。
        return

    def _write_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--failure-file", type=Path)
    args = parser.parse_args()
    EmbeddingHandler.failure_file = args.failure_file
    server = ThreadingHTTPServer((args.host, args.port), EmbeddingHandler)
    print(f"Embedding/Rerank Stub listening on {args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
