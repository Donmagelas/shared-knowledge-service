"""Knowledge API 控制面状态与租户凭证的加密持久化。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal, Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, Field

from .models import AttributeValue, IngestLastError, OperationStatus


def utc_now() -> datetime:
    """统一生成可序列化的 UTC 时间。"""

    return datetime.now(UTC)


def opaque_suffix(value: str, *, length: int = 24) -> str:
    """避免把租户 ID、Token 或幂等键写入内部 Key 和资源 ID。"""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


class KVStore(Protocol):
    """只依赖 OGX KVStore 的稳定最小接口，便于单元测试。"""

    async def set(self, key: str, value: str) -> None: ...

    async def get(self, key: str) -> str | None: ...

    async def delete(self, key: str) -> None: ...

    async def values_in_range(self, start_key: str, end_key: str) -> list[str]: ...

    async def keys_in_range(self, start_key: str, end_key: str) -> list[str]: ...


class EncryptedCredential(BaseModel):
    """PostgreSQL 中允许保存的 AES-GCM 密文结构。"""

    ciphertext: str
    nonce: str
    key_version: int = 1


class CredentialCipher:
    """用部署级 Master Key 加密租户模型凭证。"""

    def __init__(self, master_key: str) -> None:
        if len(master_key) < 16:
            raise ValueError("KNOWLEDGE_CREDENTIAL_MASTER_KEY 至少需要 16 个字符")
        # 接受 Secret 系统提供的普通字符串，再固定派生为 AES-256 Key。
        self._key = hashlib.sha256(master_key.encode("utf-8")).digest()

    def encrypt(self, value: str, *, context: str) -> EncryptedCredential:
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._key).encrypt(nonce, value.encode("utf-8"), context.encode("utf-8"))
        return EncryptedCredential(
            ciphertext=base64.urlsafe_b64encode(ciphertext).decode("ascii"),
            nonce=base64.urlsafe_b64encode(nonce).decode("ascii"),
        )

    def decrypt(self, value: EncryptedCredential, *, context: str) -> str:
        if value.key_version != 1:
            raise RuntimeError(f"不支持凭证密钥版本 {value.key_version}")
        plaintext = AESGCM(self._key).decrypt(
            base64.urlsafe_b64decode(value.nonce),
            base64.urlsafe_b64decode(value.ciphertext),
            context.encode("utf-8"),
        )
        return plaintext.decode("utf-8")


class EmbeddingProfileRecord(BaseModel):
    """一个租户唯一的 Embedding Profile。"""

    tenant_id: str
    profile_id: str
    base_url: str
    model_id: str
    dimension: int
    credential: EncryptedCredential
    locked: bool = False
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def internal_model_id(self) -> str:
        return f"tenant-inference/embedding_{opaque_suffix(self.tenant_id)}"


class RerankProfileRecord(BaseModel):
    """一个租户唯一且可随时切换的 Rerank Profile。"""

    tenant_id: str
    profile_id: str
    enabled: bool
    base_url: str | None = None
    model_id: str | None = None
    credential: EncryptedCredential | None = None
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def internal_model_id(self) -> str:
        return f"tenant-inference/rerank_{opaque_suffix(self.tenant_id)}"


class CreateIdempotencyRecord(BaseModel):
    """创建 KnowledgeBase 的可恢复幂等映射。"""

    tenant_id: str
    key_hash: str
    fingerprint: str
    knowledge_base_id: str
    state: Literal["reserved", "completed"] = "reserved"
    created_at: datetime = Field(default_factory=utc_now)


class IngestIdempotencyRecord(BaseModel):
    """单文件 Ingest 在上传和 Batch 创建之间的恢复记录。"""

    knowledge_base_id: str
    key_hash: str
    fingerprint: str
    state: Literal["reserved", "file_uploaded", "completed"] = "reserved"
    file_id: str | None = None
    operation_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class OperationRecord(BaseModel):
    """OGX FileBatch 之外需要稳定保存的文件和重试关系。"""

    operation_id: str
    knowledge_base_id: str
    file_id: str
    created_at: datetime = Field(default_factory=utc_now)
    retried_from_operation_id: str | None = None
    retried_by_operation_id: str | None = None
    status_snapshot: OperationStatus | None = None
    last_error_snapshot: IngestLastError | None = None
    terminal_at: datetime | None = None


class FileRecord(BaseModel):
    """组合 OGX File 与 VectorStoreFile 所需的公开文件元数据。"""

    knowledge_base_id: str
    file_id: str
    filename: str
    size_bytes: int
    attributes: dict[str, AttributeValue] = Field(default_factory=dict)
    latest_operation_id: str
    created_at: datetime = Field(default_factory=utc_now)


class KnowledgeBaseLifecycleRecord(BaseModel):
    """只持久化需要跨崩溃恢复的删除中状态。"""

    knowledge_base_id: str
    state: Literal["deleting"] = "deleting"
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeState:
    """在 OGX PostgreSQL KV 后端上保存统一 API 的少量控制面状态。"""

    def __init__(self, kvstore: KVStore, cipher: CredentialCipher) -> None:
        self.kvstore = kvstore
        self.cipher = cipher
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    @asynccontextmanager
    async def locked(self, key: str) -> AsyncIterator[None]:
        """单实例部署内串行化同租户、幂等键或文件生命周期。"""

        async with self._locks_guard:
            lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            yield

    @staticmethod
    def embedding_profile_id(tenant_id: str) -> str:
        return f"embp_{opaque_suffix(tenant_id)}"

    @staticmethod
    def rerank_profile_id(tenant_id: str) -> str:
        return f"rerp_{opaque_suffix(tenant_id)}"

    @staticmethod
    def _embedding_key(tenant_id: str) -> str:
        return f"profile:embedding:{opaque_suffix(tenant_id)}"

    @staticmethod
    def _rerank_key(tenant_id: str) -> str:
        return f"profile:rerank:{opaque_suffix(tenant_id)}"

    @staticmethod
    def _create_idempotency_key(tenant_id: str, idempotency_key: str) -> str:
        return f"idempotency:create:{opaque_suffix(tenant_id)}:{opaque_suffix(idempotency_key, length=40)}"

    @staticmethod
    def _ingest_idempotency_key(knowledge_base_id: str, idempotency_key: str) -> str:
        return f"idempotency:ingest:{opaque_suffix(knowledge_base_id)}:{opaque_suffix(idempotency_key, length=40)}"

    @staticmethod
    def _operation_key(knowledge_base_id: str, operation_id: str) -> str:
        return f"operation:{opaque_suffix(knowledge_base_id)}:{operation_id}"

    @staticmethod
    def _file_key(knowledge_base_id: str, file_id: str) -> str:
        return f"file:{opaque_suffix(knowledge_base_id)}:{file_id}"

    @staticmethod
    def _file_prefix(knowledge_base_id: str) -> str:
        return f"file:{opaque_suffix(knowledge_base_id)}:"

    @staticmethod
    def _knowledge_base_lifecycle_key(knowledge_base_id: str) -> str:
        return f"knowledge-base:lifecycle:{opaque_suffix(knowledge_base_id)}"

    async def get_embedding(self, tenant_id: str) -> EmbeddingProfileRecord | None:
        value = await self.kvstore.get(self._embedding_key(tenant_id))
        return EmbeddingProfileRecord.model_validate_json(value) if value else None

    async def save_embedding(self, record: EmbeddingProfileRecord) -> None:
        value = record.model_dump_json()
        await self.kvstore.set(self._embedding_key(record.tenant_id), value)
        await self.kvstore.set(f"profile:embedding-id:{record.profile_id}", value)

    async def get_embedding_by_profile(self, profile_id: str) -> EmbeddingProfileRecord | None:
        value = await self.kvstore.get(f"profile:embedding-id:{profile_id}")
        return EmbeddingProfileRecord.model_validate_json(value) if value else None

    async def get_rerank(self, tenant_id: str) -> RerankProfileRecord | None:
        value = await self.kvstore.get(self._rerank_key(tenant_id))
        return RerankProfileRecord.model_validate_json(value) if value else None

    async def save_rerank(self, record: RerankProfileRecord) -> None:
        value = record.model_dump_json()
        await self.kvstore.set(self._rerank_key(record.tenant_id), value)
        await self.kvstore.set(f"profile:rerank-id:{record.profile_id}", value)

    async def get_rerank_by_profile(self, profile_id: str) -> RerankProfileRecord | None:
        value = await self.kvstore.get(f"profile:rerank-id:{profile_id}")
        return RerankProfileRecord.model_validate_json(value) if value else None

    def encrypt_api_key(self, api_key: str, *, profile_id: str) -> EncryptedCredential:
        return self.cipher.encrypt(api_key, context=profile_id)

    def decrypt_api_key(self, credential: EncryptedCredential, *, profile_id: str) -> str:
        return self.cipher.decrypt(credential, context=profile_id)

    async def get_create_idempotency(
        self,
        tenant_id: str,
        idempotency_key: str,
    ) -> CreateIdempotencyRecord | None:
        value = await self.kvstore.get(self._create_idempotency_key(tenant_id, idempotency_key))
        return CreateIdempotencyRecord.model_validate_json(value) if value else None

    async def save_create_idempotency(self, idempotency_key: str, record: CreateIdempotencyRecord) -> None:
        await self.kvstore.set(
            self._create_idempotency_key(record.tenant_id, idempotency_key),
            record.model_dump_json(),
        )

    async def delete_create_idempotency_for_knowledge_base(self, knowledge_base_id: str) -> None:
        keys = await self.kvstore.keys_in_range("idempotency:create:", "idempotency:create:\xff")
        for key in keys:
            value = await self.kvstore.get(key)
            if value and CreateIdempotencyRecord.model_validate_json(value).knowledge_base_id == knowledge_base_id:
                await self.kvstore.delete(key)

    async def get_ingest_idempotency(
        self,
        knowledge_base_id: str,
        idempotency_key: str,
    ) -> IngestIdempotencyRecord | None:
        value = await self.kvstore.get(self._ingest_idempotency_key(knowledge_base_id, idempotency_key))
        return IngestIdempotencyRecord.model_validate_json(value) if value else None

    async def save_ingest_idempotency(self, idempotency_key: str, record: IngestIdempotencyRecord) -> None:
        await self.kvstore.set(
            self._ingest_idempotency_key(record.knowledge_base_id, idempotency_key),
            record.model_dump_json(),
        )

    async def list_ingest_idempotency(self) -> list[tuple[str, IngestIdempotencyRecord]]:
        keys = await self.kvstore.keys_in_range("idempotency:ingest:", "idempotency:ingest:\xff")
        records: list[tuple[str, IngestIdempotencyRecord]] = []
        for key in keys:
            value = await self.kvstore.get(key)
            if value:
                records.append((key, IngestIdempotencyRecord.model_validate_json(value)))
        return records

    async def delete_raw_key(self, key: str) -> None:
        """删除扫描得到的不透明内部 Key，不要求反推出调用方幂等键。"""

        await self.kvstore.delete(key)

    async def get_operation(self, knowledge_base_id: str, operation_id: str) -> OperationRecord | None:
        value = await self.kvstore.get(self._operation_key(knowledge_base_id, operation_id))
        return OperationRecord.model_validate_json(value) if value else None

    async def save_operation(self, record: OperationRecord) -> None:
        await self.kvstore.set(
            self._operation_key(record.knowledge_base_id, record.operation_id),
            record.model_dump_json(),
        )

    async def list_operations(self) -> list[OperationRecord]:
        values = await self.kvstore.values_in_range("operation:", "operation:\xff")
        return [OperationRecord.model_validate_json(value) for value in values]

    async def get_file(self, knowledge_base_id: str, file_id: str) -> FileRecord | None:
        value = await self.kvstore.get(self._file_key(knowledge_base_id, file_id))
        return FileRecord.model_validate_json(value) if value else None

    async def save_file(self, record: FileRecord) -> None:
        await self.kvstore.set(self._file_key(record.knowledge_base_id, record.file_id), record.model_dump_json())

    async def delete_file(self, knowledge_base_id: str, file_id: str) -> None:
        await self.kvstore.delete(self._file_key(knowledge_base_id, file_id))

    async def list_files(self, knowledge_base_id: str) -> list[FileRecord]:
        prefix = self._file_prefix(knowledge_base_id)
        values = await self.kvstore.values_in_range(prefix, f"{prefix}\xff")
        return [FileRecord.model_validate_json(value) for value in values]

    async def list_all_files(self) -> list[FileRecord]:
        values = await self.kvstore.values_in_range("file:", "file:\xff")
        return [FileRecord.model_validate_json(value) for value in values]

    async def file_reference_count(self, file_id: str) -> int:
        values = await self.kvstore.values_in_range("file:", "file:\xff")
        return sum(FileRecord.model_validate_json(value).file_id == file_id for value in values)

    async def get_knowledge_base_lifecycle(self, knowledge_base_id: str) -> KnowledgeBaseLifecycleRecord | None:
        value = await self.kvstore.get(self._knowledge_base_lifecycle_key(knowledge_base_id))
        return KnowledgeBaseLifecycleRecord.model_validate_json(value) if value else None

    async def mark_knowledge_base_deleting(self, knowledge_base_id: str) -> None:
        record = KnowledgeBaseLifecycleRecord(knowledge_base_id=knowledge_base_id)
        await self.kvstore.set(self._knowledge_base_lifecycle_key(knowledge_base_id), record.model_dump_json())

    async def clear_knowledge_base_lifecycle(self, knowledge_base_id: str) -> None:
        await self.kvstore.delete(self._knowledge_base_lifecycle_key(knowledge_base_id))
