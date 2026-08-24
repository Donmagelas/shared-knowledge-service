"""统一 Knowledge API Provider 需要实现的协议。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from fastapi import UploadFile

from .models import AttributeValue, IngestResponse, SearchRequest, SearchResponse


@runtime_checkable
class KnowledgeApi(Protocol):
    """Stella 与 Cherry Studio 企业版共用的两个核心业务接口。"""

    async def ingest(
        self,
        file: UploadFile,
        knowledge_base_id: str,
        attributes: dict[str, AttributeValue],
    ) -> IngestResponse: ...

    async def search(self, request: SearchRequest) -> SearchResponse: ...
