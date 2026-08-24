"""构造由 Qdrant 1.18.2 服务端编码的原生 BM25 文档。"""

from __future__ import annotations

from qdrant_client import models

NATIVE_BM25_MODEL = "qdrant/bm25"
NATIVE_BM25_OPTIONS = models.Bm25Config(
    tokenizer=models.TokenizerType.MULTILINGUAL,
    # Qdrant 1.18.2 用 none 关闭默认英文词干和停用词；升级 Qdrant 时必须重跑兼容性探针。
    language="none",
)


def native_bm25_document(text: str) -> models.Document | None:
    """返回写入和查询共用的原生 BM25 输入；无可索引字符时跳过 Sparse 路径。"""

    if not text.strip() or not any(character.isalnum() for character in text):
        return None
    return models.Document(
        text=text,
        model=NATIVE_BM25_MODEL,
        options=NATIVE_BM25_OPTIONS,
    )
