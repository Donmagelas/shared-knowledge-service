"""供 E2E 使用的 OpenAI-compatible 确定性 Embedding Stub。"""

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
    """实现测试所需的最小 `/v1/embeddings` 协议。"""

    server_version = "KnowledgeEmbeddingStub/1.0"
    failure_file: Path | None = None

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._write_json(HTTPStatus.OK, {"status": "ok"})
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/embeddings":
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
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
        self._write_json(
            HTTPStatus.OK,
            {
                "object": "list",
                "data": [
                    {"object": "embedding", "index": index, "embedding": deterministic_embedding(text)}
                    for index, text in enumerate(texts)
                ],
                "model": model,
                "usage": {"prompt_tokens": sum(len(text) for text in texts), "total_tokens": sum(map(len, texts))},
            },
        )

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
    print(f"Embedding Stub listening on {args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
