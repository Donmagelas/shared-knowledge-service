"""Qdrant 原生 multilingual BM25 输入契约测试。"""

from __future__ import annotations

import pytest
from qdrant_client import models

from shared_knowledge_service.provider.bm25 import NATIVE_BM25_MODEL, native_bm25_document


def test_native_bm25_uses_multilingual_tokenizer_without_english_processing() -> None:
    document = native_bm25_document("退款需要订单编号")

    assert document is not None
    assert document.model == NATIVE_BM25_MODEL
    assert isinstance(document.options, models.Bm25Config)
    assert document.options.tokenizer is models.TokenizerType.MULTILINGUAL
    assert document.options.language == "none"


def test_ingest_and_query_text_use_same_native_bm25_contract() -> None:
    ingest = native_bm25_document("星云Pro7保修")
    query = native_bm25_document("星云Pro7保修")

    assert ingest == query


@pytest.mark.parametrize("text", ["", "   ", "，。！？"])
def test_empty_or_punctuation_only_text_has_no_bm25_document(text: str) -> None:
    assert native_bm25_document(text) is None
