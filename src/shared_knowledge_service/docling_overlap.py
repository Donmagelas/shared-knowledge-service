"""为 OGX Docling HybridChunker 补充 token 级相邻重叠。"""

from __future__ import annotations

from typing import Any, Protocol


class ChunkTokenizer(Protocol):
    """本模块实际使用的 Docling tokenizer 最小接口。"""

    def count_tokens(self, text: str) -> int:
        """返回文本的 token 数量。"""

    def get_tokenizer(self) -> Any:
        """返回支持 offset mapping 的底层 Hugging Face tokenizer。"""


def content_token_limit(max_tokens: int, overlap_tokens: int) -> int:
    """计算 HybridChunker 可使用的新内容预算。

    ``max_tokens`` 表示最终 Chunk 上限，因此基础 Chunk 只能使用扣除 overlap
    后的预算，不能先生成 max_tokens 再额外拼接 overlap。
    """

    if max_tokens <= 0:
        raise ValueError("max_tokens 必须大于 0")
    if overlap_tokens < 0:
        raise ValueError("overlap_tokens 不能小于 0")
    if overlap_tokens >= max_tokens:
        raise ValueError("overlap_tokens 必须小于 max_tokens")
    return max_tokens - overlap_tokens


def prepend_previous_token_overlap(
    *,
    previous_text: str | None,
    current_text: str,
    tokenizer: ChunkTokenizer,
    max_tokens: int,
    overlap_tokens: int,
) -> str:
    """把上一基础 Chunk 的末尾 token 前置到当前 Chunk。

    使用 offset mapping 从原文切片，避免 decode 后改变空格、标点或表格文本。
    如果 Docling 为保持表格等原子结构已经产生超长 Chunk，则保留原 Chunk，
    不再追加 overlap，也不在这里破坏结构进行二次切分。
    """

    if not previous_text or overlap_tokens == 0:
        return current_text
    if tokenizer.count_tokens(current_text) >= max_tokens:
        return current_text

    raw_tokenizer = tokenizer.get_tokenizer()
    encoded = raw_tokenizer(previous_text, add_special_tokens=False, return_offsets_mapping=True)
    offsets = [(int(start), int(end)) for start, end in encoded["offset_mapping"] if int(end) > int(start)]
    if not offsets:
        return current_text

    # 二分寻找不突破最终上限的最大 overlap；通常一次即可命中 200 token。
    low = 1
    high = min(overlap_tokens, len(offsets))
    best = current_text
    while low <= high:
        candidate_tokens = (low + high) // 2
        start_offset = offsets[-candidate_tokens][0]
        prefix = previous_text[start_offset:].strip()
        candidate = f"{prefix}\n\n{current_text}" if prefix else current_text
        if tokenizer.count_tokens(candidate) <= max_tokens:
            best = candidate
            low = candidate_tokens + 1
        else:
            high = candidate_tokens - 1

    return best
