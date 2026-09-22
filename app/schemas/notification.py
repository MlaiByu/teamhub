"""通知 schema。

★ `title` 是**服务端渲染**出来的，不是存库字段：
  `notifications` 表只存 `type` + `payload`，标题由 `realtime.events`
  的模板在读取时生成。好处是措辞可以随版本演进（改文案不需要数据迁移），
  代价是渲染要做容错（旧类型 / 旧结构，见 `render_title`）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class NotificationOut(BaseModel):
    """一条通知。字段与 WebSocket 推送的消息体保持一致，前端共用一套渲染逻辑。"""

    id: int
    event: str
    title: str
    payload: dict[str, Any]
    is_read: bool
    read_at: datetime | None
    created_at: datetime


class NotificationReadResult(BaseModel):
    """标记已读的结果。"""

    updated: int


class UnreadCountOut(BaseModel):
    """未读数。前端角标用。"""

    unread: int
