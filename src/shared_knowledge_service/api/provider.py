"""复用 OGX 对象、任务和自定义 Qdrant Provider 的 Knowledge API 实现。"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi import UploadFile
from ogx.core.storage.datatypes import KVStoreReference
from ogx.core.storage.kvstore import kvstore_impl
from ogx.log import get_logger
from ogx.providers.utils.inference.prompt_adapter import interleaved_content_as_str
from ogx.providers.utils.vector_io.filters import parse_filter
from ogx_api import (
    Api,
    DeleteFileRequest,
    Files,
    InlineProviderSpec,
    ListFilesRequest,
    ModelType,
    OpenAICreateVectorStoreFileBatchRequestWithExtraBody,
    OpenAICreateVectorStoreRequestWithExtraBody,
    OpenAIFileObjectNotFoundError,
    OpenAIUpdateVectorStoreRequest,
    QueryChunksResponse,
    RetrieveFileRequest,
    UploadFileRequest,
    VectorIO,
    VectorStoreNotFoundError,
)
from ogx_api.files.models import OpenAIFileUploadPurpose
from pydantic import BaseModel, ConfigDict, Field

from shared_knowledge_service.provider.adapter import SharedQdrantVectorIOAdapter
from shared_knowledge_service.provider.attributes import encode_attributes_for_ogx
from shared_knowledge_service.provider.filtering import FilterTranslationError, payload_field_path

from .errors import ApiSecurity, KnowledgeError
from .models import (
    AttributeValue,
    EmbeddingConfigPutRequest,
    EmbeddingConfigResponse,
    FileCounts,
    FileDetail,
    FileItem,
    FileQueryRequest,
    FileQueryResponse,
    IngestLastError,
    IngestResponse,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseEmbedding,
    KnowledgeBaseResponse,
    OperationResponse,
    OperationStatus,
    RerankConfigPutRequest,
    RerankConfigResponse,
    RetryOperationResponse,
    SearchHit,
    SearchRequest,
    SearchResponse,
)
from .state import (
    CreateIdempotencyRecord,
    CredentialCipher,
    EmbeddingProfileRecord,
    FileRecord,
    IngestIdempotencyRecord,
    KnowledgeState,
    OperationRecord,
    RerankProfileRecord,
    opaque_suffix,
    utc_now,
)
from .upstream import InferenceUrlPolicy, probe_embedding, probe_rerank

log = get_logger(name=__name__, category="providers")

RESERVED_INGEST_ATTRIBUTES = frozenset(
    {
        "attributes",
        "chunk_content",
        "chunk_id",
        "chunk_window",
        "content_text",
        "document_id",
        "embedding_dimension",
        "embedding_model",
        "file_id",
        "filename",
        "headings",
        "source",
        "tenant_id",
        "vector_store_id",
    }
)

_SEARCH_INTERNAL_METADATA = frozenset(
    {
        "chunk_id",
        "chunk_window",
        "document_id",
        "file_id",
        "filename",
        "headings",
        "source",
        "vector_store_id",
    }
)


def _normalize_identifier(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise KnowledgeError(422, "invalid_request", f"{field_name} 不能为空")
    return normalized


def _datetime_from_timestamp(value: int | float) -> datetime:
    return datetime.fromtimestamp(value, tz=UTC)


def _request_fingerprint(parts: list[bytes]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def _cursor_encode(record: FileRecord) -> str:
    raw = json.dumps(
        {"created_at": record.created_at.isoformat(), "file_id": record.file_id},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _cursor_decode(value: str) -> tuple[datetime, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        created_at = datetime.fromisoformat(decoded["created_at"])
        file_id = decoded["file_id"]
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise KnowledgeError(422, "invalid_cursor", "文件分页 cursor 不合法") from exc
    if created_at.tzinfo is None or not isinstance(file_id, str) or not file_id:
        raise KnowledgeError(422, "invalid_cursor", "文件分页 cursor 不合法")
    return created_at, file_id


def _eq_value(actual: AttributeValue | str | None, expected: object) -> bool:
    if isinstance(actual, list):
        return expected in actual
    return actual == expected


def _matches_file_filter(record: FileRecord, filter_: dict[str, Any]) -> bool:
    """用与 Qdrant Match 语义一致的方式筛选文件元数据。"""

    filter_type = filter_["type"]
    if filter_type in {"and", "or"}:
        values = [_matches_file_filter(record, child) for child in filter_["filters"]]
        return all(values) if filter_type == "and" else any(values)
    key = filter_["key"]
    actual: AttributeValue | str | None = record.file_id if key == "file_id" else record.attributes.get(key)
    expected = filter_["value"]
    if filter_type == "eq":
        return _eq_value(actual, expected)
    if filter_type == "ne":
        return not _eq_value(actual, expected)
    if filter_type in {"in", "nin"}:
        if not isinstance(expected, list):
            return False
        matched = any(_eq_value(actual, item) for item in expected)
        return matched if filter_type == "in" else not matched
    if isinstance(actual, list) or not isinstance(actual, int | float) or isinstance(actual, bool):
        return False
    if not isinstance(expected, int | float) or isinstance(expected, bool):
        return False
    return {
        "gt": actual > expected,
        "gte": actual >= expected,
        "lt": actual < expected,
        "lte": actual <= expected,
    }[filter_type]


def _validate_file_filter_fields(filter_: dict[str, Any]) -> None:
    """文件查询复用 Search 的公开字段边界，但不接受 Chunk 专属字段。"""

    if filter_["type"] in {"and", "or"}:
        for child in filter_["filters"]:
            _validate_file_filter_fields(child)
        return
    key = filter_["key"]
    if key == "chunk_id":
        raise FilterTranslationError("文件查询不能按 chunk_id 过滤")
    payload_field_path(key)


class KnowledgeApiConfig(BaseModel):
    """统一 API 控制面、凭证安全和服务 Token 配置。"""

    model_config = ConfigDict(extra="forbid")

    persistence: KVStoreReference
    credential_master_key: str = Field(min_length=16)
    runtime_token: str = Field(min_length=16)
    admin_token: str = Field(min_length=16)
    allowed_inference_hosts: str | None = None
    http_allowed_inference_hosts: str | None = None
    inference_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    failed_source_retention_days: int = Field(default=7, ge=0, le=3650)
    uncommitted_source_retention_hours: int = Field(default=24, ge=0, le=8760)
    file_cleanup_interval_hours: float = Field(default=24.0, gt=0, le=8760)


class KnowledgeApiProvider:
    """把产品稳定接口转换成 OGX 原生对象与租户共享 Qdrant 查询。"""

    def __init__(
        self,
        files_api: Files,
        vector_io: VectorIO,
        *,
        inference_api: Any | None = None,
        state: KnowledgeState | None = None,
        security: ApiSecurity | None = None,
        url_policy: InferenceUrlPolicy | None = None,
        inference_timeout_seconds: float = 30.0,
        failed_source_retention_days: int = 7,
        uncommitted_source_retention_hours: int = 24,
        file_cleanup_interval_hours: float = 24.0,
    ) -> None:
        self.files_api = files_api
        self.vector_io = vector_io
        self.inference_api = inference_api
        self.state = state
        self.security = security or ApiSecurity("runtime-local-only", "admin-local-only")
        self.url_policy = url_policy or InferenceUrlPolicy()
        self.inference_timeout_seconds = inference_timeout_seconds
        self.failed_source_retention = timedelta(days=failed_source_retention_days)
        self.uncommitted_source_retention = timedelta(hours=uncommitted_source_retention_hours)
        self.file_cleanup_interval_seconds = file_cleanup_interval_hours * 3600
        self._cleanup_task: asyncio.Task[None] | None = None

    async def initialize(self) -> None:
        """启动现有进程内的无效原文件清理循环，不新增 Worker 或公开 API。"""

        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="knowledge-file-cleanup")

    async def shutdown(self) -> None:
        """优雅停止清理循环；每个清理步骤本身保持幂等。"""

        if self._cleanup_task is None:
            return
        self._cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._cleanup_task
        self._cleanup_task = None

    def _state(self) -> KnowledgeState:
        if self.state is None:
            raise RuntimeError("Knowledge API 控制面状态尚未配置")
        return self.state

    async def _shared_provider_and_stores(
        self,
        knowledge_base_ids: list[str],
        *,
        allow_deleting: bool = False,
    ) -> tuple[SharedQdrantVectorIOAdapter, list[Any]]:
        """校验逻辑知识库存在，并解析到同一个共享 Qdrant Provider。"""

        if not allow_deleting:
            for knowledge_base_id in knowledge_base_ids:
                lifecycle = await self._state().get_knowledge_base_lifecycle(knowledge_base_id)
                if lifecycle is not None:
                    raise KnowledgeError(409, "knowledge_base_deleting", "KnowledgeBase 正在删除")
        routing_table = getattr(self.vector_io, "routing_table", None)
        if routing_table is None or not hasattr(routing_table, "get_provider_impl"):
            raise RuntimeError("VectorIO 没有可用的 OGX RoutingTable")
        providers: list[Any] = []
        stores: list[Any] = []
        for knowledge_base_id in knowledge_base_ids:
            try:
                stores.append(await self.vector_io.openai_retrieve_vector_store(knowledge_base_id))
                providers.append(await routing_table.get_provider_impl(knowledge_base_id))
            except (VectorStoreNotFoundError, KeyError, ValueError) as exc:
                raise KnowledgeError(404, "knowledge_base_not_found", "KnowledgeBase 不存在") from exc
        first = providers[0]
        if not isinstance(first, SharedQdrantVectorIOAdapter):
            raise KnowledgeError(422, "invalid_knowledge_base", "KnowledgeBase 未使用 shared-qdrant Provider")
        if any(provider is not first for provider in providers[1:]):
            raise KnowledgeError(
                422,
                "knowledge_base_route_mismatch",
                "一次请求中的 KnowledgeBase 必须属于同一 Provider",
            )
        return first, stores

    async def _shared_provider(self, knowledge_base_ids: list[str]) -> SharedQdrantVectorIOAdapter:
        provider, _ = await self._shared_provider_and_stores(knowledge_base_ids)
        return provider

    @staticmethod
    def _tenant_id(store: Any) -> str:
        metadata = store.metadata or {}
        tenant_id = metadata.get("tenant_id")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise RuntimeError("KnowledgeBase 缺少可信 tenant_id")
        return tenant_id

    async def _ensure_internal_model(
        self,
        *,
        internal_model_id: str,
        profile_id: str,
        model_type: ModelType,
        dimension: int | None = None,
    ) -> None:
        """注册不暴露真实 URL、Key 或上游模型名的 OGX 内部资源。"""

        if self.inference_api is None:
            raise RuntimeError("Knowledge API 没有 Inference 依赖")
        routing_table = getattr(self.inference_api, "routing_table", None)
        existing = None
        if routing_table is not None:
            existing = await routing_table.get_object_by_identifier("model", internal_model_id)
        metadata = {"embedding_dimension": dimension} if dimension is not None else {}
        if existing is None:
            await self.inference_api.register_model(
                model_id=internal_model_id,
                provider_model_id=profile_id,
                provider_id="tenant-inference",
                metadata=metadata,
                model_type=model_type,
            )
            return
        if existing.provider_id != "tenant-inference" or existing.provider_resource_id != profile_id:
            raise RuntimeError("内部租户模型资源与 Profile 映射冲突")
        if dimension is not None and existing.metadata.get("embedding_dimension") != dimension:
            if routing_table is None:
                raise RuntimeError("Inference 没有可用的 OGX RoutingTable")
            updated = existing.model_copy(update={"metadata": metadata})
            await routing_table.dist_registry.register(updated)

    async def _sync_empty_store_embeddings(self, profile: EmbeddingProfileRecord) -> None:
        routing_table = getattr(self.vector_io, "routing_table", None)
        if routing_table is None:
            raise RuntimeError("VectorIO 没有可用的 OGX RoutingTable")
        provider = routing_table.impls_by_provider_id.get("shared-qdrant")
        if not isinstance(provider, SharedQdrantVectorIOAdapter):
            raise RuntimeError("shared-qdrant Provider 未注册")
        changed = await provider.reconfigure_empty_tenant_stores(
            profile.tenant_id,
            embedding_model=profile.internal_model_id,
            embedding_dimension=profile.dimension,
        )
        for knowledge_base_id in changed:
            resource = await routing_table.get_object_by_identifier("vector_store", knowledge_base_id)
            if resource is None:
                continue
            updated = resource.model_copy(
                update={
                    "embedding_model": profile.internal_model_id,
                    "embedding_dimension": profile.dimension,
                }
            )
            await routing_table.dist_registry.register(updated)

    @staticmethod
    def _embedding_response(profile: EmbeddingProfileRecord) -> EmbeddingConfigResponse:
        return EmbeddingConfigResponse(
            base_url=profile.base_url,
            model_id=profile.model_id,
            dimension=profile.dimension,
            credential_configured=True,
            locked=profile.locked,
            updated_at=profile.updated_at,
        )

    async def put_embedding_config(
        self,
        tenant_id: str,
        request: EmbeddingConfigPutRequest,
    ) -> EmbeddingConfigResponse:
        tenant_id = _normalize_identifier(tenant_id, field_name="tenant_id")
        state = self._state()
        async with state.locked(f"tenant:{opaque_suffix(tenant_id)}"):
            existing = await state.get_embedding(tenant_id)
            if existing is None and request.api_key is None:
                raise KnowledgeError(422, "embedding_api_key_required", "首次配置 Embedding 必须提供 api_key")
            normalized_url = await self.url_policy.normalize_and_validate(request.base_url)
            if existing is not None and existing.locked:
                locked_values = (existing.base_url, existing.model_id, existing.dimension)
                requested_values = (normalized_url, request.model_id, request.dimension)
                if requested_values != locked_values:
                    raise KnowledgeError(409, "embedding_config_locked", "租户 Embedding 向量空间已经锁定")
            profile_id = existing.profile_id if existing else state.embedding_profile_id(tenant_id)
            credential = existing.credential if existing and request.api_key is None else None
            if request.api_key is not None:
                credential = state.encrypt_api_key(request.api_key, profile_id=profile_id)
            if credential is None:
                raise KnowledgeError(422, "embedding_api_key_required", "Embedding api_key 尚未配置")
            effective_key = request.api_key or state.decrypt_api_key(credential, profile_id=profile_id)
            await probe_embedding(
                policy=self.url_policy,
                base_url=normalized_url,
                api_key=effective_key,
                model_id=request.model_id,
                dimension=request.dimension,
                timeout_seconds=self.inference_timeout_seconds,
            )
            profile = EmbeddingProfileRecord(
                tenant_id=tenant_id,
                profile_id=profile_id,
                base_url=normalized_url,
                model_id=request.model_id,
                dimension=request.dimension,
                credential=credential,
                locked=existing.locked if existing else False,
                updated_at=utc_now(),
            )
            await state.save_embedding(profile)
            await self._ensure_internal_model(
                internal_model_id=profile.internal_model_id,
                profile_id=profile.profile_id,
                model_type=ModelType.embedding,
                dimension=profile.dimension,
            )
            if not profile.locked:
                await self._sync_empty_store_embeddings(profile)
            return self._embedding_response(profile)

    async def get_embedding_config(self, tenant_id: str) -> EmbeddingConfigResponse:
        tenant_id = _normalize_identifier(tenant_id, field_name="tenant_id")
        profile = await self._state().get_embedding(tenant_id)
        if profile is None:
            raise KnowledgeError(404, "embedding_config_not_found", "租户 Embedding 配置不存在")
        return self._embedding_response(profile)

    @staticmethod
    def _rerank_response(profile: RerankProfileRecord) -> RerankConfigResponse:
        return RerankConfigResponse(
            enabled=profile.enabled,
            base_url=profile.base_url,
            model_id=profile.model_id,
            credential_configured=profile.credential is not None,
            updated_at=profile.updated_at,
        )

    async def put_rerank_config(
        self,
        tenant_id: str,
        request: RerankConfigPutRequest,
    ) -> RerankConfigResponse:
        tenant_id = _normalize_identifier(tenant_id, field_name="tenant_id")
        state = self._state()
        async with state.locked(f"tenant-rerank:{opaque_suffix(tenant_id)}"):
            existing = await state.get_rerank(tenant_id)
            profile_id = existing.profile_id if existing else state.rerank_profile_id(tenant_id)
            base_url = request.base_url if request.base_url is not None else (existing.base_url if existing else None)
            model_id = request.model_id if request.model_id is not None else (existing.model_id if existing else None)
            credential = existing.credential if existing else None
            if request.api_key is not None:
                credential = state.encrypt_api_key(request.api_key, profile_id=profile_id)
            normalized_url = await self.url_policy.normalize_and_validate(base_url) if base_url is not None else None
            if request.enabled and (normalized_url is None or model_id is None or credential is None):
                raise KnowledgeError(422, "rerank_config_incomplete", "启用 Rerank 需要完整 URL、api_key 和 model_id")
            if request.enabled:
                api_key = request.api_key or state.decrypt_api_key(cast(Any, credential), profile_id=profile_id)
                await probe_rerank(
                    policy=self.url_policy,
                    base_url=cast(str, normalized_url),
                    api_key=api_key,
                    model_id=cast(str, model_id),
                    timeout_seconds=self.inference_timeout_seconds,
                )
            profile = RerankProfileRecord(
                tenant_id=tenant_id,
                profile_id=profile_id,
                enabled=request.enabled,
                base_url=normalized_url,
                model_id=model_id,
                credential=credential,
                updated_at=utc_now(),
            )
            await state.save_rerank(profile)
            if profile.base_url is not None and profile.model_id is not None and profile.credential is not None:
                await self._ensure_internal_model(
                    internal_model_id=profile.internal_model_id,
                    profile_id=profile.profile_id,
                    model_type=ModelType.rerank,
                )
            return self._rerank_response(profile)

    async def get_rerank_config(self, tenant_id: str) -> RerankConfigResponse:
        tenant_id = _normalize_identifier(tenant_id, field_name="tenant_id")
        profile = await self._state().get_rerank(tenant_id)
        if profile is None:
            raise KnowledgeError(404, "rerank_config_not_found", "租户 Rerank 配置不存在")
        return self._rerank_response(profile)

    async def _create_vector_store_with_id(
        self,
        knowledge_base_id: str,
        profile: EmbeddingProfileRecord,
    ) -> Any:
        """预分配 ID，保证创建幂等记录可以在 OGX 对象之前落盘。"""

        routing_table = getattr(self.vector_io, "routing_table", None)
        if routing_table is None:
            raise RuntimeError("VectorIO 没有可用的 OGX RoutingTable")
        resource = await routing_table.get_object_by_identifier("vector_store", knowledge_base_id)
        if resource is None:
            resource = await routing_table.register_vector_store(
                vector_store_id=knowledge_base_id,
                embedding_model=profile.internal_model_id,
                embedding_dimension=profile.dimension,
                provider_id="shared-qdrant",
                provider_vector_store_id=knowledge_base_id,
            )
        provider = await routing_table.get_provider_impl(resource.identifier)
        try:
            existing = await provider.openai_retrieve_vector_store(knowledge_base_id)
        except VectorStoreNotFoundError:
            params = OpenAICreateVectorStoreRequestWithExtraBody.model_validate(
                {
                    "metadata": {"tenant_id": profile.tenant_id},
                    "provider_vector_store_id": knowledge_base_id,
                    "provider_id": "shared-qdrant",
                    "embedding_model": profile.internal_model_id,
                    "embedding_dimension": profile.dimension,
                }
            )
            return await provider.openai_create_vector_store(params)
        metadata = dict(existing.metadata or {})
        if metadata.get("tenant_id") != profile.tenant_id:
            return await provider.openai_update_vector_store(
                knowledge_base_id,
                OpenAIUpdateVectorStoreRequest(
                    metadata={
                        **metadata,
                        "tenant_id": profile.tenant_id,
                        "embedding_model": profile.internal_model_id,
                        "embedding_dimension": str(profile.dimension),
                    }
                ),
            )
        return existing

    async def create_knowledge_base(
        self,
        request: KnowledgeBaseCreateRequest,
        idempotency_key: str,
    ) -> KnowledgeBaseResponse:
        idempotency_key = _normalize_identifier(idempotency_key, field_name="Idempotency-Key")
        if len(idempotency_key) > 255:
            raise KnowledgeError(422, "invalid_idempotency_key", "Idempotency-Key 不能超过 255 个字符")
        state = self._state()
        profile = await state.get_embedding(request.tenant_id)
        if profile is None:
            raise KnowledgeError(409, "embedding_config_required", "创建 KnowledgeBase 前必须配置 Embedding")
        await self._ensure_internal_model(
            internal_model_id=profile.internal_model_id,
            profile_id=profile.profile_id,
            model_type=ModelType.embedding,
            dimension=profile.dimension,
        )
        fingerprint = _request_fingerprint([request.tenant_id.encode("utf-8")])
        lock_key = f"create:{opaque_suffix(request.tenant_id)}:{opaque_suffix(idempotency_key)}"
        async with state.locked(lock_key):
            record = await state.get_create_idempotency(request.tenant_id, idempotency_key)
            if record is not None and record.fingerprint != fingerprint:
                raise KnowledgeError(409, "idempotency_conflict", "Idempotency-Key 已用于不同请求")
            if record is None:
                record = CreateIdempotencyRecord(
                    tenant_id=request.tenant_id,
                    key_hash=opaque_suffix(idempotency_key, length=40),
                    fingerprint=fingerprint,
                    knowledge_base_id=f"vs_{uuid.uuid4()}",
                )
                await state.save_create_idempotency(idempotency_key, record)
            store = await self._create_vector_store_with_id(record.knowledge_base_id, profile)
            replayed = record.state == "completed"
            if not replayed:
                record.state = "completed"
                await state.save_create_idempotency(idempotency_key, record)
            response = await self._knowledge_base_response(store)
            response.replayed = replayed
            return response

    async def _knowledge_base_response(self, store: Any) -> KnowledgeBaseResponse:
        tenant_id = self._tenant_id(store)
        profile = await self._state().get_embedding(tenant_id)
        if profile is None:
            raise RuntimeError("KnowledgeBase 对应的 Embedding Profile 不存在")
        counts = await self._file_counts(store.id)
        return KnowledgeBaseResponse(
            knowledge_base_id=store.id,
            tenant_id=tenant_id,
            embedding=KnowledgeBaseEmbedding(
                model_id=profile.model_id,
                dimension=profile.dimension,
                locked=profile.locked,
            ),
            file_counts=counts,
            created_at=_datetime_from_timestamp(store.created_at),
        )

    async def get_knowledge_base(self, knowledge_base_id: str) -> KnowledgeBaseResponse:
        normalized_id = _normalize_identifier(knowledge_base_id, field_name="knowledge_base_id")
        _, stores = await self._shared_provider_and_stores([normalized_id])
        return await self._knowledge_base_response(stores[0])

    async def _file_counts(self, knowledge_base_id: str) -> FileCounts:
        records = await self._state().list_files(knowledge_base_id)
        counts = {"processing": 0, "completed": 0, "failed": 0}
        for record in records:
            operation = await self.get_ingest_operation(knowledge_base_id, record.latest_operation_id)
            key = "failed" if operation.status == "cancelled" else operation.status
            counts[key] += 1
        return FileCounts(total=len(records), **counts)

    async def _prepare_embedding_for_ingest(
        self,
        tenant_id: str,
        provider: SharedQdrantVectorIOAdapter,
        knowledge_base_id: str,
    ) -> EmbeddingProfileRecord:
        """在同一租户锁内确认 Collection 后锁定向量空间配置。"""

        state = self._state()
        async with state.locked(f"tenant:{opaque_suffix(tenant_id)}"):
            profile = await state.get_embedding(tenant_id)
            if profile is None:
                raise KnowledgeError(409, "embedding_config_required", "Ingest 前必须配置 Embedding")
            # Qdrant 初始化失败时请求尚未被接受，不应提前把租户配置永久锁死。
            await provider.ensure_vector_store_collection(knowledge_base_id)
            if not profile.locked:
                profile.locked = True
                profile.updated_at = utc_now()
                await state.save_embedding(profile)
            return profile

    async def ingest(
        self,
        file: UploadFile,
        knowledge_base_id: str,
        attributes: dict[str, AttributeValue],
        idempotency_key: str,
    ) -> IngestResponse:
        knowledge_base_id = _normalize_identifier(knowledge_base_id, field_name="knowledge_base_id")
        idempotency_key = _normalize_identifier(idempotency_key, field_name="Idempotency-Key")
        forbidden = RESERVED_INGEST_ATTRIBUTES.intersection(attributes)
        if forbidden:
            raise KnowledgeError(
                422,
                "reserved_attribute",
                "attributes 不能覆盖保留字段",
                {"fields": sorted(forbidden)},
            )
        content = await file.read()
        await file.seek(0)
        filename = file.filename or "upload.bin"
        canonical_attributes = json.dumps(attributes, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = _request_fingerprint(
            [content, filename.encode("utf-8"), knowledge_base_id.encode("utf-8"), canonical_attributes.encode("utf-8")]
        )
        return await self._ingest_serialized(
            file=file,
            knowledge_base_id=knowledge_base_id,
            attributes=attributes,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
        )

    async def _ingest_serialized(
        self,
        *,
        file: UploadFile,
        knowledge_base_id: str,
        attributes: dict[str, AttributeValue],
        idempotency_key: str,
        fingerprint: str,
    ) -> IngestResponse:
        """与 KnowledgeBase 删除串行化，并完成可恢复的单文件提交。"""

        state = self._state()
        knowledge_base_lock = f"knowledge-base:{opaque_suffix(knowledge_base_id)}"
        idempotency_lock = f"ingest:{opaque_suffix(knowledge_base_id)}:{opaque_suffix(idempotency_key)}"
        async with state.locked(knowledge_base_lock):
            if await state.get_knowledge_base_lifecycle(knowledge_base_id) is not None:
                raise KnowledgeError(409, "knowledge_base_deleting", "KnowledgeBase 正在删除")
            async with state.locked(idempotency_lock):
                record = await state.get_ingest_idempotency(knowledge_base_id, idempotency_key)
                if record is not None and record.fingerprint != fingerprint:
                    raise KnowledgeError(409, "idempotency_conflict", "Idempotency-Key 已用于不同文件或参数")
                if record is not None and record.state == "completed" and record.file_id and record.operation_id:
                    operation = await self.get_ingest_operation(knowledge_base_id, record.operation_id)
                    return IngestResponse(
                        operation_id=record.operation_id,
                        file_id=record.file_id,
                        knowledge_base_id=knowledge_base_id,
                        status=operation.status,
                        replayed=True,
                    )
                provider, stores = await self._shared_provider_and_stores([knowledge_base_id])
                tenant_id = self._tenant_id(stores[0])
                profile = await self._prepare_embedding_for_ingest(tenant_id, provider, knowledge_base_id)
                if record is None:
                    record = IngestIdempotencyRecord(
                        knowledge_base_id=knowledge_base_id,
                        key_hash=opaque_suffix(idempotency_key, length=40),
                        fingerprint=fingerprint,
                    )
                    await state.save_ingest_idempotency(idempotency_key, record)
                if record.file_id is None:
                    uploaded = await self.files_api.openai_upload_file(
                        UploadFileRequest(purpose=OpenAIFileUploadPurpose.ASSISTANTS),
                        file,
                    )
                    record.file_id = uploaded.id
                    record.state = "file_uploaded"
                    await state.save_ingest_idempotency(idempotency_key, record)
                else:
                    try:
                        uploaded = await self.files_api.openai_retrieve_file(
                            RetrieveFileRequest(file_id=record.file_id)
                        )
                    except OpenAIFileObjectNotFoundError as exc:
                        raise KnowledgeError(409, "ingest_source_missing", "幂等请求对应的原文件不存在") from exc
                    except Exception as exc:
                        raise KnowledgeError(503, "file_storage_unavailable", "原文件存储暂时不可用") from exc
                encoded_attributes = encode_attributes_for_ogx(attributes)
                batch = provider.find_single_file_batch(knowledge_base_id, uploaded.id, encoded_attributes)
                if batch is None:
                    try:
                        batch = await self.vector_io.openai_create_vector_store_file_batch(
                            vector_store_id=knowledge_base_id,
                            params=OpenAICreateVectorStoreFileBatchRequestWithExtraBody(
                                file_ids=[uploaded.id],
                                attributes=encoded_attributes,
                            ),
                        )
                    except Exception as exc:
                        raise KnowledgeError(
                            503,
                            "ingest_operation_unavailable",
                            "原文件已保存，但异步导入任务暂时无法创建",
                            {"file_id": uploaded.id},
                        ) from exc
                operation_record = OperationRecord(
                    operation_id=batch.id,
                    knowledge_base_id=knowledge_base_id,
                    file_id=uploaded.id,
                )
                await state.save_operation(operation_record)
                await state.save_file(
                    FileRecord(
                        knowledge_base_id=knowledge_base_id,
                        file_id=uploaded.id,
                        filename=uploaded.filename,
                        size_bytes=uploaded.bytes,
                        attributes=attributes,
                        latest_operation_id=batch.id,
                        created_at=_datetime_from_timestamp(uploaded.created_at),
                    )
                )
                record.operation_id = batch.id
                record.state = "completed"
                await state.save_ingest_idempotency(idempotency_key, record)
                log.info(
                    "Knowledge ingest accepted",
                    vector_store_id=knowledge_base_id,
                    tenant_profile_id=profile.profile_id,
                    operation_id=batch.id,
                    file_id=uploaded.id,
                )
                return IngestResponse(
                    operation_id=batch.id,
                    file_id=uploaded.id,
                    knowledge_base_id=knowledge_base_id,
                    status="processing",
                )

    @staticmethod
    def _operation_status(batch_status: str, file_counts: dict[str, int]) -> OperationStatus:
        if batch_status == "in_progress":
            return "processing"
        if batch_status == "cancelled":
            return "cancelled"
        if batch_status == "failed" or file_counts.get("failed", 0) > 0:
            return "failed"
        if batch_status == "completed" and file_counts.get("completed", 0) == file_counts.get("total", 0) == 1:
            return "completed"
        return "failed"

    async def _operation_last_error(self, knowledge_base_id: str, operation_id: str) -> IngestLastError:
        try:
            files = await self.vector_io.openai_list_files_in_vector_store_file_batch(
                batch_id=operation_id,
                vector_store_id=knowledge_base_id,
                limit=1,
            )
        except Exception:
            return IngestLastError(code="operation_failed", message="异步导入失败，文件级错误读取失败")
        if files.data and files.data[0].last_error is not None:
            error = files.data[0].last_error
            return IngestLastError(code=error.code, message=error.message)
        return IngestLastError(code="operation_failed", message="异步导入失败，但未返回文件级错误")

    async def get_ingest_operation(
        self,
        knowledge_base_id: str,
        operation_id: str,
    ) -> OperationResponse:
        knowledge_base_id = _normalize_identifier(knowledge_base_id, field_name="knowledge_base_id")
        operation_id = _normalize_identifier(operation_id, field_name="operation_id")
        record = await self._state().get_operation(knowledge_base_id, operation_id)
        if record is None:
            raise KnowledgeError(404, "operation_not_found", "Operation 不存在")
        status: OperationStatus
        last_error: IngestLastError | None
        try:
            batch = await self.vector_io.openai_retrieve_vector_store_file_batch(
                batch_id=operation_id,
                vector_store_id=knowledge_base_id,
            )
        except Exception as exc:
            if record.status_snapshot is None:
                # 控制面记录证明 Operation 存在；底层 Batch 暂时不可读不能伪装成 404。
                raise KnowledgeError(503, "operation_state_unavailable", "Operation 状态暂时不可用") from exc
            status = record.status_snapshot
            last_error = record.last_error_snapshot
        else:
            status = self._operation_status(batch.status, batch.file_counts.model_dump())
            last_error = (
                await self._operation_last_error(knowledge_base_id, operation_id) if status == "failed" else None
            )
            if status != "processing" and (
                record.status_snapshot != status or record.last_error_snapshot != last_error
            ):
                record.status_snapshot = status
                record.last_error_snapshot = last_error
                record.terminal_at = record.terminal_at or utc_now()
                await self._state().save_operation(record)
        source_exists = await self._raw_file_exists(record.file_id)
        retryable = (
            status == "failed"
            and source_exists
            and record.retried_by_operation_id is None
            and not await self._file_has_processing_operation(knowledge_base_id, record.file_id)
        )
        return OperationResponse(
            operation_id=operation_id,
            knowledge_base_id=knowledge_base_id,
            file_id=record.file_id,
            status=status,
            created_at=record.created_at,
            last_error=last_error,
            retryable=retryable,
            retried_from_operation_id=record.retried_from_operation_id,
            retried_by_operation_id=record.retried_by_operation_id,
        )

    async def _raw_file_exists(self, file_id: str) -> bool:
        try:
            await self.files_api.openai_retrieve_file(RetrieveFileRequest(file_id=file_id))
        except (OpenAIFileObjectNotFoundError, KeyError):
            return False
        except Exception as exc:
            raise KnowledgeError(503, "file_storage_unavailable", "原文件存储暂时不可用") from exc
        return True

    async def _file_has_processing_operation(self, knowledge_base_id: str, file_id: str) -> bool:
        file_record = await self._state().get_file(knowledge_base_id, file_id)
        if file_record is None:
            return False
        operation = await self._state().get_operation(knowledge_base_id, file_record.latest_operation_id)
        if operation is None:
            return False
        if operation.status_snapshot is not None:
            return False
        try:
            batch = await self.vector_io.openai_retrieve_vector_store_file_batch(
                batch_id=operation.operation_id,
                vector_store_id=knowledge_base_id,
            )
        except ValueError:
            # OGX 对过期或已清理的 Batch 统一抛 ValueError。Provider 内仍保留
            # 当前实际在途 Batch 时必须阻止删除；不存在则可视为已不再处理。
            provider = await self._shared_provider([knowledge_base_id])
            return provider.has_in_progress_file_batch(knowledge_base_id, file_id)
        except Exception as exc:
            # 无法证明任务已经终止时宁可暂缓删除，不能误删仍在处理的文件。
            raise KnowledgeError(503, "operation_state_unavailable", "Operation 状态暂时不可用") from exc
        return self._operation_status(batch.status, batch.file_counts.model_dump()) == "processing"

    async def retry_ingest_operation(
        self,
        knowledge_base_id: str,
        operation_id: str,
    ) -> RetryOperationResponse:
        state = self._state()
        async with state.locked(f"knowledge-base:{opaque_suffix(knowledge_base_id)}"):
            if await state.get_knowledge_base_lifecycle(knowledge_base_id) is not None:
                raise KnowledgeError(409, "knowledge_base_deleting", "KnowledgeBase 正在删除")
            return await self._retry_ingest_under_knowledge_base_lock(knowledge_base_id, operation_id)

    async def _retry_ingest_under_knowledge_base_lock(
        self,
        knowledge_base_id: str,
        operation_id: str,
    ) -> RetryOperationResponse:
        """在 KB 生命周期锁内恢复或创建唯一的直接重试子 Operation。"""

        state = self._state()
        lock_key = f"retry:{opaque_suffix(knowledge_base_id)}:{operation_id}"
        async with state.locked(lock_key):
            old = await state.get_operation(knowledge_base_id, operation_id)
            if old is None:
                raise KnowledgeError(404, "operation_not_found", "Operation 不存在")
            if old.retried_by_operation_id is not None:
                child = await self.get_ingest_operation(knowledge_base_id, old.retried_by_operation_id)
                file_record = await state.get_file(knowledge_base_id, old.file_id)
                if file_record is not None and file_record.latest_operation_id != child.operation_id:
                    file_record.latest_operation_id = child.operation_id
                    await state.save_file(file_record)
                return RetryOperationResponse(
                    operation_id=child.operation_id,
                    knowledge_base_id=knowledge_base_id,
                    file_id=child.file_id,
                    status=child.status,
                    retried_from_operation_id=operation_id,
                    replayed=True,
                )
            current = await self.get_ingest_operation(knowledge_base_id, operation_id)
            if current.status != "failed":
                raise KnowledgeError(409, "operation_not_retryable", "只有最终失败的 Operation 可以重试")
            file_record = await state.get_file(knowledge_base_id, old.file_id)
            if file_record is None or not await self._raw_file_exists(old.file_id):
                raise KnowledgeError(409, "retry_source_missing", "重试所需原文件不存在")
            try:
                await self.vector_io.openai_delete_vector_store_file(knowledge_base_id, old.file_id)
                # 失败挂载可能已被上次重试清理；后续单文件 Batch 仍是幂等恢复入口。
            except (ValueError, KeyError):
                pass
            except Exception as exc:
                raise KnowledgeError(503, "vector_store_unavailable", "失败索引清理暂时不可用") from exc
            provider = await self._shared_provider([knowledge_base_id])
            encoded_attributes = encode_attributes_for_ogx(file_record.attributes)
            batch = provider.find_single_file_batch(
                knowledge_base_id,
                old.file_id,
                encoded_attributes,
                excluded_batch_ids={operation_id},
            )
            if batch is None:
                batch = await self.vector_io.openai_create_vector_store_file_batch(
                    vector_store_id=knowledge_base_id,
                    params=OpenAICreateVectorStoreFileBatchRequestWithExtraBody(
                        file_ids=[old.file_id],
                        attributes=encoded_attributes,
                    ),
                )
            child_record = OperationRecord(
                operation_id=batch.id,
                knowledge_base_id=knowledge_base_id,
                file_id=old.file_id,
                retried_from_operation_id=operation_id,
            )
            old.retried_by_operation_id = batch.id
            file_record.latest_operation_id = batch.id
            await state.save_operation(old)
            await state.save_operation(child_record)
            await state.save_file(file_record)
            return RetryOperationResponse(
                operation_id=batch.id,
                knowledge_base_id=knowledge_base_id,
                file_id=old.file_id,
                status="processing",
                retried_from_operation_id=operation_id,
            )

    async def _file_item(self, record: FileRecord) -> FileItem:
        operation = await self.get_ingest_operation(record.knowledge_base_id, record.latest_operation_id)
        return FileItem(
            file_id=record.file_id,
            filename=record.filename,
            size_bytes=record.size_bytes,
            status=operation.status,
            latest_operation_id=record.latest_operation_id,
            attributes=record.attributes,
            last_error=operation.last_error,
            created_at=record.created_at,
        )

    async def query_files(self, knowledge_base_id: str, request: FileQueryRequest) -> FileQueryResponse:
        await self._shared_provider([knowledge_base_id])
        if request.filters is not None:
            try:
                _validate_file_filter_fields(request.filters)
            except FilterTranslationError as exc:
                raise KnowledgeError(422, "invalid_filter", str(exc)) from exc
        records = await self._state().list_files(knowledge_base_id)
        records.sort(key=lambda item: (item.created_at, item.file_id), reverse=True)
        if request.cursor:
            cursor = _cursor_decode(request.cursor)
            records = [record for record in records if (record.created_at, record.file_id) < cursor]
        if request.filters is not None:
            records = [record for record in records if _matches_file_filter(record, request.filters)]
        items: list[FileItem] = []
        last_record: FileRecord | None = None
        has_more = False
        for record in records:
            item = await self._file_item(record)
            if request.statuses is not None and item.status not in request.statuses:
                continue
            if len(items) == request.limit:
                has_more = True
                break
            items.append(item)
            last_record = record
        return FileQueryResponse(
            items=items,
            next_cursor=_cursor_encode(last_record) if has_more and last_record is not None else None,
            has_more=has_more,
        )

    async def get_file(self, knowledge_base_id: str, file_id: str) -> FileDetail:
        await self._shared_provider([knowledge_base_id])
        record = await self._state().get_file(knowledge_base_id, file_id)
        if record is None:
            raise KnowledgeError(404, "file_not_found", "文件不存在")
        item = await self._file_item(record)
        return FileDetail(knowledge_base_id=knowledge_base_id, **item.model_dump())

    async def delete_file(self, knowledge_base_id: str, file_id: str) -> None:
        state = self._state()
        async with state.locked(f"knowledge-base:{opaque_suffix(knowledge_base_id)}"):
            if await state.get_knowledge_base_lifecycle(knowledge_base_id) is not None:
                raise KnowledgeError(409, "knowledge_base_deleting", "KnowledgeBase 正在删除")
            await self._delete_file_under_knowledge_base_lock(knowledge_base_id, file_id)

    async def _delete_file_under_knowledge_base_lock(self, knowledge_base_id: str, file_id: str) -> None:
        """供单文件删除、整库删除和内部清理复用，调用方必须先持有 KB 锁。"""

        state = self._state()
        async with (
            state.locked(f"file:{opaque_suffix(knowledge_base_id)}:{file_id}"),
            state.locked(f"raw-file:{file_id}"),
        ):
            record = await state.get_file(knowledge_base_id, file_id)
            if record is None:
                # 重复删除只能确认技术挂载已不存在，不能借此删除任意全局 File。
                return
            if await self._file_has_processing_operation(knowledge_base_id, file_id):
                raise KnowledgeError(
                    409,
                    "file_busy",
                    "文件仍在处理中",
                    {"active_operation_id": record.latest_operation_id},
                )
            try:
                await self.vector_io.openai_delete_vector_store_file(knowledge_base_id, file_id)
            except (ValueError, KeyError):
                # 解析失败可能尚未形成 VectorStoreFile；删除仍可继续。
                pass
            except Exception as exc:
                raise KnowledgeError(503, "vector_store_unavailable", "文件索引清理暂时失败") from exc
            # 当前记录尚未删除，引用数为 1 表示它是原 File 的最后一个挂载。
            if await state.file_reference_count(file_id) == 1 and await self._raw_file_exists(file_id):
                try:
                    await self.files_api.openai_delete_file(DeleteFileRequest(file_id=file_id))
                except Exception as exc:
                    raise KnowledgeError(503, "file_storage_unavailable", "原文件清理暂时失败") from exc
            await state.delete_file(knowledge_base_id, file_id)

    async def delete_knowledge_base(self, knowledge_base_id: str) -> None:
        state = self._state()
        async with state.locked(f"knowledge-base:{opaque_suffix(knowledge_base_id)}"):
            lifecycle = await state.get_knowledge_base_lifecycle(knowledge_base_id)
            store_exists = True
            try:
                await self._shared_provider_and_stores([knowledge_base_id], allow_deleting=True)
            except KnowledgeError as exc:
                if exc.code != "knowledge_base_not_found":
                    raise
                store_exists = False
                if lifecycle is None:
                    return
            files = await state.list_files(knowledge_base_id)
            if lifecycle is None:
                active: list[str] = []
                for record in files:
                    if await self._file_has_processing_operation(knowledge_base_id, record.file_id):
                        active.append(record.latest_operation_id)
                if active:
                    raise KnowledgeError(
                        409,
                        "knowledge_base_busy",
                        "KnowledgeBase 仍有正在处理的导入任务",
                        {"active_operation_ids": active},
                    )
                await state.mark_knowledge_base_deleting(knowledge_base_id)
            for record in files:
                await self._delete_file_under_knowledge_base_lock(knowledge_base_id, record.file_id)
            if store_exists:
                await self.vector_io.openai_delete_vector_store(knowledge_base_id)
            await state.delete_create_idempotency_for_knowledge_base(knowledge_base_id)
            await state.clear_knowledge_base_lifecycle(knowledge_base_id)

    async def _cleanup_loop(self) -> None:
        """启动即扫描，此后按部署间隔回收无效原文件。"""

        while True:
            try:
                await self.cleanup_invalid_files()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 清理失败不能影响在线 Ingest/Search；下一轮继续幂等重试。
                log.exception("Knowledge file cleanup failed", error_type=type(exc).__name__)
            await asyncio.sleep(self.file_cleanup_interval_seconds)

    async def _list_raw_files(self) -> list[Any]:
        """分页读取 OGX Files，兼容本地卷与 S3 Provider。"""

        result: list[Any] = []
        after: str | None = None
        while True:
            page = await self.files_api.openai_list_files(
                ListFilesRequest(
                    after=after,
                    limit=10_000,
                    purpose=OpenAIFileUploadPurpose.ASSISTANTS,
                )
            )
            result.extend(page.data)
            if not page.has_more or not page.last_id:
                return result
            after = page.last_id

    async def _cleanup_uncommitted_ingests(self, now: datetime) -> int:
        """回收超过保留期、尚未形成稳定 Operation 的请求。"""

        state = self._state()
        cutoff = now - self.uncommitted_source_retention
        cleaned = 0
        for storage_key, record in await state.list_ingest_idempotency():
            if record.state == "completed" or record.created_at > cutoff:
                continue
            if record.file_id is not None:
                try:
                    provider = await self._shared_provider([record.knowledge_base_id])
                except KnowledgeError as exc:
                    if exc.code != "knowledge_base_not_found":
                        raise
                    provider = None
                if provider is not None and provider.has_in_progress_file_batch(
                    record.knowledge_base_id, record.file_id
                ):
                    continue
                if provider is not None:
                    with contextlib.suppress(ValueError, KeyError):
                        await self.vector_io.openai_delete_vector_store_file(
                            record.knowledge_base_id,
                            record.file_id,
                        )
                if await state.file_reference_count(record.file_id) == 0 and await self._raw_file_exists(
                    record.file_id
                ):
                    await self.files_api.openai_delete_file(DeleteFileRequest(file_id=record.file_id))
            await state.delete_raw_key(storage_key)
            cleaned += 1
        return cleaned

    async def _cleanup_failed_files(self, now: datetime) -> int:
        """按最新 Operation 的首次终态时间回收失败或取消的文件挂载。"""

        state = self._state()
        cutoff = now - self.failed_source_retention
        cleaned = 0
        for record in await state.list_all_files():
            try:
                operation = await self.get_ingest_operation(record.knowledge_base_id, record.latest_operation_id)
            except KnowledgeError as exc:
                if exc.status_code in {404, 503}:
                    continue
                raise
            if operation.status not in {"failed", "cancelled"}:
                continue
            persisted = await state.get_operation(record.knowledge_base_id, record.latest_operation_id)
            if persisted is None or persisted.terminal_at is None or persisted.terminal_at > cutoff:
                continue
            async with state.locked(f"knowledge-base:{opaque_suffix(record.knowledge_base_id)}"):
                if await state.get_knowledge_base_lifecycle(record.knowledge_base_id) is not None:
                    continue
                await self._delete_file_under_knowledge_base_lock(record.knowledge_base_id, record.file_id)
            cleaned += 1
        return cleaned

    async def _cleanup_orphan_files(self, now: datetime) -> int:
        """回收没有任何统一控制面引用的 OGX File。"""

        state = self._state()
        cutoff = now - self.uncommitted_source_retention
        file_records = await state.list_all_files()
        operations = await state.list_operations()
        idempotency = await state.list_ingest_idempotency()
        referenced = {record.file_id for record in file_records}
        referenced.update(record.file_id for record in operations)
        referenced.update(record.file_id for _, record in idempotency if record.file_id is not None)
        cleaned = 0
        for raw_file in await self._list_raw_files():
            if raw_file.id in referenced or _datetime_from_timestamp(raw_file.created_at) > cutoff:
                continue
            await self.files_api.openai_delete_file(DeleteFileRequest(file_id=raw_file.id))
            cleaned += 1
        return cleaned

    async def cleanup_invalid_files(self, *, now: datetime | None = None) -> dict[str, int]:
        """执行一次幂等清理，返回内部计数供测试和日志使用。"""

        scan_time = now or utc_now()
        uncommitted = await self._cleanup_uncommitted_ingests(scan_time)
        failed = await self._cleanup_failed_files(scan_time)
        orphan = await self._cleanup_orphan_files(scan_time)
        if uncommitted or failed or orphan:
            log.info(
                "Knowledge file cleanup completed",
                uncommitted=uncommitted,
                failed=failed,
                orphan=orphan,
            )
        return {"uncommitted": uncommitted, "failed": failed, "orphan": orphan}

    async def search(self, request: SearchRequest) -> SearchResponse:
        provider, stores = await self._shared_provider_and_stores(request.knowledge_base_ids)
        tenant_ids = {self._tenant_id(store) for store in stores}
        if len(tenant_ids) != 1:
            raise KnowledgeError(422, "cross_tenant_search", "一次 Search 不能跨租户 Collection")
        rerank_model: str | None = None
        if request.mode == "hybrid":
            rerank = await self._state().get_rerank(next(iter(tenant_ids)))
            if rerank is not None and rerank.enabled:
                await self._ensure_internal_model(
                    internal_model_id=rerank.internal_model_id,
                    profile_id=rerank.profile_id,
                    model_type=ModelType.rerank,
                )
                rerank_model = rerank.internal_model_id
        try:
            filters = parse_filter(request.filters) if request.filters is not None else None
            result = await provider.query_multiple_vector_stores(
                vector_store_ids=request.knowledge_base_ids,
                query=request.query,
                mode=request.mode,
                limit=request.limit,
                filters=filters,
                rerank_model=rerank_model,
            )
        except ValueError as exc:
            raise KnowledgeError(422, "invalid_search", str(exc)) from exc
        return SearchResponse(hits=self._search_hits(result))

    @staticmethod
    def _search_hits(result: QueryChunksResponse) -> list[SearchHit]:
        hits: list[SearchHit] = []
        for chunk, score in zip(result.chunks, result.scores, strict=True):
            metadata = dict(chunk.metadata or {})
            file_id = str(
                metadata.get("file_id")
                or (chunk.chunk_metadata.document_id if chunk.chunk_metadata else None)
                or metadata.get("document_id")
                or ""
            )
            knowledge_base_id = str(metadata.get("vector_store_id") or "")
            source = chunk.chunk_metadata.source if chunk.chunk_metadata else ""
            filename = str(metadata.get("filename") or source or "")
            locator: dict[str, Any] = {}
            if chunk.chunk_metadata:
                if chunk.chunk_metadata.source:
                    locator["source"] = chunk.chunk_metadata.source
                if chunk.chunk_metadata.chunk_window is not None:
                    locator["chunk_window"] = chunk.chunk_metadata.chunk_window
            headings = metadata.get("headings")
            if isinstance(headings, list) and all(isinstance(item, str) for item in headings):
                locator["headings"] = headings
            attributes = {
                key: cast(AttributeValue, value)
                for key, value in metadata.items()
                if key not in _SEARCH_INTERNAL_METADATA
                and (
                    isinstance(value, str | int | float | bool)
                    or (isinstance(value, list) and all(isinstance(item, str | int | float | bool) for item in value))
                )
            }
            hits.append(
                SearchHit(
                    knowledge_base_id=knowledge_base_id,
                    file_id=file_id,
                    filename=filename,
                    chunk_id=chunk.chunk_id,
                    content=interleaved_content_as_str(chunk.content),
                    locator=locator,
                    score=score,
                    attributes=attributes,
                )
            )
        return hits


def get_provider_spec() -> InlineProviderSpec:
    """返回统一 Knowledge API 的 OGX Provider 规格。"""

    return InlineProviderSpec(
        api=Api("knowledge"),
        provider_type="inline::shared-knowledge",
        config_class="shared_knowledge_service.api.provider.KnowledgeApiConfig",
        module="shared_knowledge_service.api",
        api_dependencies=[Api.files, Api.vector_io, Api.inference],
        is_external=True,
        description="Stella and Cherry Studio Enterprise shared Knowledge API.",
    )


def available_providers() -> list[InlineProviderSpec]:
    """让 OGX 为外部 API 发现内置的唯一实现。"""

    return [get_provider_spec()]


async def get_provider_impl(config: KnowledgeApiConfig, deps: dict[Api, Any]) -> KnowledgeApiProvider:
    """从 OGX 注入 Files、VectorIO、Inference 和 PostgreSQL 控制面存储。"""

    kvstore = await kvstore_impl(config.persistence)
    state = KnowledgeState(kvstore, CredentialCipher(config.credential_master_key))
    impl = KnowledgeApiProvider(
        files_api=deps[Api.files],
        vector_io=deps[Api.vector_io],
        inference_api=deps[Api.inference],
        state=state,
        security=ApiSecurity(config.runtime_token, config.admin_token),
        url_policy=InferenceUrlPolicy.from_csv(
            allowed_hosts=config.allowed_inference_hosts,
            http_allowed_hosts=config.http_allowed_inference_hosts,
        ),
        inference_timeout_seconds=config.inference_timeout_seconds,
        failed_source_retention_days=config.failed_source_retention_days,
        uncommitted_source_retention_hours=config.uncommitted_source_retention_hours,
        file_cleanup_interval_hours=config.file_cleanup_interval_hours,
    )
    await impl.initialize()
    return impl
