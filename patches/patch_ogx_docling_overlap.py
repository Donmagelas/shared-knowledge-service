"""在镜像构建时为固定 OGX 1.3.0 Docling Provider 应用 overlap 补丁。"""

from __future__ import annotations

import importlib.util
from importlib.metadata import version
from pathlib import Path

EXPECTED_OGX_VERSION = "1.3.0"
MODULE_NAME = "ogx.providers.inline.file_processor.docling.docling"


def replace_once(source: str, old: str, new: str, label: str) -> str:
    """只替换唯一匹配；OGX 源码漂移时让镜像构建立刻失败。"""

    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"无法应用 OGX Docling 补丁 {label}：预期 1 处，实际 {count} 处")
    return source.replace(old, new, 1)


def main() -> int:
    """定位已安装模块并应用最小源码补丁。"""

    installed_version = version("ogx")
    if installed_version != EXPECTED_OGX_VERSION:
        raise RuntimeError(f"OGX 版本不匹配：预期 {EXPECTED_OGX_VERSION}，实际 {installed_version}")

    spec = importlib.util.find_spec(MODULE_NAME)
    if spec is None or spec.origin is None:
        raise RuntimeError(f"找不到 OGX Docling 模块：{MODULE_NAME}")

    target = Path(spec.origin)
    source = target.read_text(encoding="utf-8")
    import_line = (
        "from shared_knowledge_service.docling_overlap import (\n"
        "    content_token_limit,\n"
        "    prepend_previous_token_overlap,\n"
        ")\n"
    )
    source = replace_once(
        source,
        "from fastapi import UploadFile\n",
        f"from fastapi import UploadFile\n\n{import_line}",
        "导入 overlap helper",
    )

    source = replace_once(
        source,
        """        # Determine max_tokens based on strategy
        if chunking_strategy.type == "auto":
            max_tokens = self.config.default_chunk_size_tokens
        elif chunking_strategy.type == "static":
            max_tokens = chunking_strategy.static.max_chunk_size_tokens
        else:
            max_tokens = self.config.default_chunk_size_tokens

        # max_tokens is set on the tokenizer, not on HybridChunker directly
""",
        """        # 同时解析最终 Chunk 上限和相邻重叠；上游 OGX 1.3.0 原本只读取前者。
        if chunking_strategy.type == "auto":
            max_tokens = self.config.default_chunk_size_tokens
            overlap_tokens = self.config.default_chunk_overlap_tokens
        elif chunking_strategy.type == "static":
            max_tokens = chunking_strategy.static.max_chunk_size_tokens
            overlap_tokens = chunking_strategy.static.chunk_overlap_tokens
        else:
            max_tokens = self.config.default_chunk_size_tokens
            overlap_tokens = self.config.default_chunk_overlap_tokens

        # HybridChunker 只生成新内容，最终上限还要为前一 Chunk 的 overlap 预留预算。
        base_max_tokens = content_token_limit(max_tokens, overlap_tokens)

        # max_tokens is set on the tokenizer, not on HybridChunker directly
""",
        "读取 overlap 配置",
    )
    source = replace_once(
        source,
        """        tokenizer = HuggingFaceTokenizer(
            tokenizer=default_chunker.tokenizer.tokenizer,  # type: ignore[attr-defined]
            max_tokens=max_tokens,
        )
""",
        """        tokenizer = HuggingFaceTokenizer(
            tokenizer=default_chunker.tokenizer.tokenizer,  # type: ignore[attr-defined]
            max_tokens=base_max_tokens,
        )
""",
        "设置基础 Chunk 预算",
    )
    source = replace_once(
        source,
        """        chunks: list[Chunk] = []
        for i, doc_chunk in enumerate(doc_chunks):
            text = doc_chunk.text
            if not text or not text.strip():
                continue

            headings = getattr(doc_chunk, "headings", None)
            chunk_window = f"{i}"

            chunk_id = generate_chunk_id(document_id, text, chunk_window)

            meta: dict[str, Any] = {
                "document_id": document_id,
                **document_metadata,
            }
            if headings:
                meta["headings"] = headings

            chunks.append(
                Chunk(
                    content=text,
                    chunk_id=chunk_id,
                    metadata=meta,
                    chunk_metadata=ChunkMetadata(
                        chunk_id=chunk_id,
                        document_id=document_id,
                        source=document_metadata.get("filename", ""),
                        content_token_count=len(text.split()),
                        chunk_window=chunk_window,
                    ),
                )
            )

        return chunks
""",
        """        chunks: list[Chunk] = []
        previous_text: str | None = None
        for i, doc_chunk in enumerate(doc_chunks):
            base_text = doc_chunk.text
            if not base_text or not base_text.strip():
                continue

            # 只引用上一基础 Chunk，避免 overlap 在后续 Chunk 中递归传播。
            text = prepend_previous_token_overlap(
                previous_text=previous_text,
                current_text=base_text,
                tokenizer=tokenizer,
                max_tokens=max_tokens,
                overlap_tokens=overlap_tokens,
            )
            previous_text = base_text

            headings = getattr(doc_chunk, "headings", None)
            chunk_window = f"{i}"

            chunk_id = generate_chunk_id(document_id, text, chunk_window)

            meta: dict[str, Any] = {
                "document_id": document_id,
                **document_metadata,
            }
            if headings:
                meta["headings"] = headings

            chunks.append(
                Chunk(
                    content=text,
                    chunk_id=chunk_id,
                    metadata=meta,
                    chunk_metadata=ChunkMetadata(
                        chunk_id=chunk_id,
                        document_id=document_id,
                        source=document_metadata.get("filename", ""),
                        content_token_count=tokenizer.count_tokens(text),
                        chunk_window=chunk_window,
                    ),
                )
            )

        return chunks
""",
        "生成带 overlap 的 Chunk",
    )

    target.write_text(source, encoding="utf-8")
    print(f"已为 {target} 应用 OGX Docling token overlap 补丁")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
