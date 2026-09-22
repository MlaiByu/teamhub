"""实时通知的事件类型定义与 payload 契约。

★ 为什么每个事件类型配一个**独立的 payload 模型**，而不是一个裸 dict：

  事件 payload 有两条去处——**落库**（`notifications.payload`，JSONB）
  和**推给前端**。用裸 dict 的话，「任务分配」与「状态变更」的字段差异
  只能靠注释维护，写入端与消费端（通知列表渲染、前端）各写一遍字段名，
  改名时必然漂移，而且是**运行期才暴露**的漂移。

  每个类型一个模型后：
    - `emit` 时按类型校验，字段写错立刻报错
    - 通知列表读回时按类型取标题模板，读写共用同一份契约
    - `extra="forbid"` 挡住「顺手往 payload 里塞字段」——payload 会进数据库，
      放任它变成一个什么都装的袋子，半年后没人说得清里面有什么

★ 新增事件类型的完整清单（这就是「完整实现一个事件」的定义）：
    1. 在 `EventType` 加枚举成员
    2. 写它自己的 Payload 模型
    3. 在 `EVENT_SPECS` 注册（绑定模型 + 人类可读标题模板）
    4. 在业务触发点调用 `notification_service.emit(...)`
    5. 补测试：触发后通知落库且内容正确；**且不该发的时候没发**
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.logging import get_logger

logger = get_logger(__name__)


class EventType(StrEnum):
    """全部通知事件类型。

    取值用 `领域.动作` 的点分形式：`notifications.type` 是 String(32)，
    点分形式既能一眼看出所属领域，又留了足够长度。
    """

    TASK_ASSIGNED = "task.assigned"
    TASK_STATUS_CHANGED = "task.status_changed"
    TASK_COMMENTED = "task.commented"
    TASK_MENTIONED = "task.mentioned"
    MEMBER_JOINED = "member.joined"
    ROLE_ASSIGNED = "role.assigned"


class _Payload(BaseModel):
    """所有 payload 的基类。

    `extra="forbid"`：payload 会原样进数据库并推给前端，
    必须是**封闭**的结构，不能随手加字段。

    ★ 字段名不得与信封字段重名——见 `ENVELOPE_RESERVED_FIELDS`。
      重名不会报「字段重复」这种明显的错，而是让 `NotificationEvent.create`
      的同名关键字被信封吃掉，payload 侧表现为「Field required」，
      排查时容易往错误方向找。
    """

    model_config = ConfigDict(extra="forbid")


# `NotificationEvent.create(...)` 的这些关键字归**信封**所有，
# 任何 payload 模型都不许用同名字段。
ENVELOPE_RESERVED_FIELDS: frozenset[str] = frozenset(
    {"event_type", "type", "tenant_id", "target_user_id", "actor_id", "payload", "created_at"}
)


# ----------------------------------------------------------------------
# 逐事件类型的 payload —— 每个类型一份，字段按该事件真正需要的信息定义
# ----------------------------------------------------------------------
class TaskAssignedPayload(_Payload):
    """任务分配给你（含改派）。"""

    task_id: int
    task_title: str
    project_id: int
    project_name: str
    assigned_by: int | None = None
    # 区分「首次分配」与「改派」，前端措辞不同
    reassigned: bool = False


class TaskStatusChangedPayload(_Payload):
    """你负责的任务状态变了。"""

    task_id: int
    task_title: str
    from_status: str
    to_status: str
    changed_by: int | None = None


class TaskCommentedPayload(_Payload):
    """你的任务被评论了。"""

    task_id: int
    task_title: str
    comment_id: int
    excerpt: str
    author_id: int


class TaskMentionedPayload(_Payload):
    """评论里 @ 了你。"""

    task_id: int
    task_title: str
    comment_id: int
    excerpt: str
    mentioned_by: int


class MemberJoinedPayload(_Payload):
    """你被加入了某个团队。

    ★ 这里**不放** `tenant_id`：它在信封上已经有了（`NotificationEvent.tenant_id`）。
      放了反而会出事——`NotificationEvent.create(tenant_id=...)` 的关键字
      会被信封先吃掉，payload 里的同名字段就永远取不到值，直接在构造时报
      「Field required」。信封字段与 payload 字段不能重名，见下方的
      `ENVELOPE_RESERVED_FIELDS` 与对应的结构性自检测试。
    """

    tenant_name: str
    role_codes: list[str] = []


class RoleAssignedPayload(_Payload):
    """你被授予了一个角色。"""

    role_id: int
    role_code: str
    role_name: str
    data_scope: str


# ----------------------------------------------------------------------
# 注册表：类型 → (payload 模型, 标题模板)
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class EventSpec:
    """一个事件类型的完整描述。"""

    type: EventType
    payload_model: type[_Payload]
    # 标题模板的占位符必须与 payload 字段同名——渲染时按 payload 取
    title_template: str


EVENT_SPECS: dict[EventType, EventSpec] = {
    EventType.TASK_ASSIGNED: EventSpec(
        type=EventType.TASK_ASSIGNED,
        payload_model=TaskAssignedPayload,
        title_template="任务「{task_title}」已分配给你",
    ),
    EventType.TASK_STATUS_CHANGED: EventSpec(
        type=EventType.TASK_STATUS_CHANGED,
        payload_model=TaskStatusChangedPayload,
        title_template="任务「{task_title}」状态由 {from_status} 变为 {to_status}",
    ),
    EventType.TASK_COMMENTED: EventSpec(
        type=EventType.TASK_COMMENTED,
        payload_model=TaskCommentedPayload,
        title_template="任务「{task_title}」有新评论：{excerpt}",
    ),
    EventType.TASK_MENTIONED: EventSpec(
        type=EventType.TASK_MENTIONED,
        payload_model=TaskMentionedPayload,
        title_template="在任务「{task_title}」的评论中有人 @ 了你：{excerpt}",
    ),
    EventType.MEMBER_JOINED: EventSpec(
        type=EventType.MEMBER_JOINED,
        payload_model=MemberJoinedPayload,
        title_template="你已加入团队「{tenant_name}」",
    ),
    EventType.ROLE_ASSIGNED: EventSpec(
        type=EventType.ROLE_ASSIGNED,
        payload_model=RoleAssignedPayload,
        title_template="你被授予角色「{role_name}」",
    ),
}


# ----------------------------------------------------------------------
# 构造 / 渲染
# ----------------------------------------------------------------------
class UnknownEventTypeError(ValueError):
    """事件类型未注册。

    继承 ValueError 而非直接抛 KeyError：调用方通常传的是字符串，
    「值不合法」比「键不存在」更贴近语义。
    """


def get_spec(event_type: EventType | str) -> EventSpec:
    """取事件规格。未注册则报错——**这是开发期错误，不该静默降级**。"""
    try:
        key = EventType(event_type)
    except ValueError as exc:
        raise UnknownEventTypeError(f"未注册的事件类型：{event_type!r}") from exc
    spec = EVENT_SPECS.get(key)
    if spec is None:
        raise UnknownEventTypeError(f"事件类型 {key} 在 EVENT_SPECS 里没有登记")
    return spec


def build_payload(event_type: EventType | str, **data: Any) -> dict[str, Any]:
    """按事件类型校验并序列化 payload。

    ★ 校验放在这里，而不是让调用方自己拼 dict：
      emit 的调用点是散在各 service 里的，漏字段/写错字段名只会在
      前端渲染或数据回读时才暴露；在这一处校验能立刻把问题钉在触发点。
    """
    spec = get_spec(event_type)
    validated = spec.payload_model(**data)
    return validated.model_dump(mode="json")


def render_title(event_type: str, payload: dict[str, Any]) -> str:
    """把事件渲染成一句人话，用于通知列表。

    ★ 容错是**刻意**的：`notifications` 里可能躺着历史行——
      要么是事件类型后来被下线了，要么是 payload 结构演进过。
      渲染失败不该让整个列表接口 500，所以降级成通用文案并留一条 warning
      （warning 是为了让漂移能被发现，而不是被静默吞掉）。
    """
    try:
        spec = get_spec(event_type)
    except UnknownEventTypeError:
        logger.warning("render_title_unknown_type", event_type=event_type)
        return "有一条新通知"

    try:
        return spec.title_template.format(**payload)
    except (KeyError, IndexError):
        logger.warning("render_title_payload_mismatch", event_type=event_type)
        return "有一条新通知"


def all_event_types() -> list[EventType]:
    """全部已注册事件类型。用于文档、测试与自检。"""
    return list(EVENT_SPECS)


@dataclass(frozen=True)
class NotificationEvent:
    """一条待投递的通知事件（内存中的形态，尚未落库）。

    ★ 它把「谁该收到」与「内容是什么」绑在一起，但不含数据库主键——
      落库由 `notification_service.emit` 负责，推送用的是同一份对象。
      这样「落库的内容」与「推送的内容」必然一致，不会出现
      库里写着 A、前端看到 B 的分歧。
    """

    type: EventType
    tenant_id: int
    target_user_id: int
    actor_id: int | None
    payload: dict[str, Any]
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        event_type: EventType | str,
        tenant_id: int,
        target_user_id: int,
        actor_id: int | None = None,
        **payload: Any,
    ) -> NotificationEvent:
        """构造事件：顺带完成 payload 校验。"""
        return cls(
            type=get_spec(event_type).type,
            tenant_id=tenant_id,
            target_user_id=target_user_id,
            actor_id=actor_id,
            payload=build_payload(event_type, **payload),
            created_at=datetime.now(UTC),
        )

    def to_message(self) -> dict[str, Any]:
        """转成推送给 WebSocket 的消息体（与通知列表的字段保持一致）。"""
        return {
            "event": str(self.type),
            "tenant_id": self.tenant_id,
            "user_id": self.target_user_id,
            "actor_id": self.actor_id,
            "title": render_title(str(self.type), self.payload),
            "payload": self.payload,
            "created_at": self.created_at.isoformat(),
        }


__all__ = [
    "EventType",
    "EventSpec",
    "EVENT_SPECS",
    "ENVELOPE_RESERVED_FIELDS",
    "NotificationEvent",
    "UnknownEventTypeError",
    "get_spec",
    "build_payload",
    "render_title",
    "all_event_types",
]
