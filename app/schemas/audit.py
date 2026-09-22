"""审计 schema。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class AuditLogOut(BaseModel):
    """一条审计日志。

    ★ `detail` 原样回吐是**危险**的：它是 JSONB，将来若某个触发点不小心
      写进了敏感字段（如旧密码、令牌），查询接口会把它吐给所有管理员。
      但完全不给 detail 又失去了审计「看细节」的价值。折中：这里回吐
      `detail`，但**入口只对 TENANT_ADMIN 开放**（见路由的 require_roles），
      且约定 detail 只存最小键集（见 audit_service docstring）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int | None
    action: str
    entity_type: str
    entity_id: int | None
    detail: dict[str, Any]
    ip: str | None
    created_at: datetime
