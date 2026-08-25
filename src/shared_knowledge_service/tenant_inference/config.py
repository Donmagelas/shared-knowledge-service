"""租户感知 Inference Provider 配置。"""

from __future__ import annotations

from ogx.core.storage.datatypes import KVStoreReference
from pydantic import BaseModel, Field


class TenantInferenceConfig(BaseModel):
    """只保存设施级安全配置，不保存任何租户 API Key。"""

    persistence: KVStoreReference
    credential_master_key: str = Field(min_length=16)
    allowed_hosts: str | None = None
    http_allowed_hosts: str | None = None
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)
