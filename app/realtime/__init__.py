"""实时通信：连接管理与事件定义。

    events.py   事件类型、payload 契约、渲染
    manager.py  按 (tenant_id, user_id) 维护 WebSocket 连接并投递

★ 本包**只依赖 core**，不 import api / services：
  它被 services 调用（发通知），如果反过来依赖 services 就成环了。
"""

from app.realtime.events import (
    EVENT_SPECS,
    EventSpec,
    EventType,
    NotificationEvent,
    UnknownEventTypeError,
    all_event_types,
    build_payload,
    get_spec,
    render_title,
)
from app.realtime.manager import ConnectionManager, manager

__all__ = [
    # events
    "EventType",
    "EventSpec",
    "EVENT_SPECS",
    "NotificationEvent",
    "UnknownEventTypeError",
    "get_spec",
    "build_payload",
    "render_title",
    "all_event_types",
    # manager
    "ConnectionManager",
    "manager",
]
