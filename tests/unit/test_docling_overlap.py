"""Docling token overlap 的纯函数测试。"""

from __future__ import annotations

import re
from typing import Any

import pytest

from shared_knowledge_service.docling_overlap import content_token_limit, prepend_previous_token_overlap


class FakeRawTokenizer:
    """使用空白词边界模拟 Hugging Face fast tokenizer 的 offset mapping。"""

    def __call__(self, text: str, **_: Any) -> dict[str, list[tuple[int, int]]]:
        return {"offset_mapping": [(match.start(), match.end()) for match in re.finditer(r"\S+", text)]}


class FakeChunkTokenizer:
    """为单元测试提供稳定、无模型依赖的 token 计数器。"""

    def count_tokens(self, text: str) -> int:
        return len(re.findall(r"\S+", text))

    def get_tokenizer(self) -> FakeRawTokenizer:
        return FakeRawTokenizer()


def test_content_token_limit_reserves_overlap_from_final_limit() -> None:
    assert content_token_limit(1000, 200) == 800


@pytest.mark.parametrize("max_tokens,overlap_tokens", [(0, 0), (100, -1), (100, 100), (100, 101)])
def test_content_token_limit_rejects_invalid_values(max_tokens: int, overlap_tokens: int) -> None:
    with pytest.raises(ValueError):
        content_token_limit(max_tokens, overlap_tokens)


def test_prepend_previous_token_overlap_uses_previous_tail_without_exceeding_limit() -> None:
    tokenizer = FakeChunkTokenizer()
    result = prepend_previous_token_overlap(
        previous_text="p1 p2 p3 p4 p5",
        current_text="c1 c2 c3 c4 c5 c6 c7",
        tokenizer=tokenizer,
        max_tokens=10,
        overlap_tokens=4,
    )

    # 当前 Chunk 只剩 3 token 空间，因此不会盲目加入配置中的全部 4 token。
    assert result == "p3 p4 p5\n\nc1 c2 c3 c4 c5 c6 c7"
    assert tokenizer.count_tokens(result) == 10


def test_prepend_previous_token_overlap_does_not_split_oversized_structural_chunk() -> None:
    tokenizer = FakeChunkTokenizer()
    current = "c1 c2 c3 c4 c5"

    assert (
        prepend_previous_token_overlap(
            previous_text="p1 p2",
            current_text=current,
            tokenizer=tokenizer,
            max_tokens=5,
            overlap_tokens=2,
        )
        == current
    )
