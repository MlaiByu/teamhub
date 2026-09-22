"""事件类型定义测试（第 4 周步骤 1·事件侧）。

★ 本文件里有两条**结构性自检**，价值高于普通行为测试：

  1. `test_every_event_type_is_registered` —— 遍历 `EventType` 的每个成员，
     断言都已在 `EVENT_SPECS` 登记。**新增事件类型忘了注册**会让
     `get_spec` 抛错，而那是在 emit 的运行期才发现；这条把它拦在测试期。

  2. `test_title_template_only_uses_payload_fields` —— 断言标题模板里的
     占位符都真实存在于对应 payload 模型上。模板写成 `{task_titel}` 这种
     拼写错误，平时只会让用户看到「有一条新通知」的降级文案，很难发现。

  这两条都属于「用测试把约定钉死」，比多写几个 happy-path 用例更值。
"""

from __future__ import annotations

import string

import pytest
from pydantic import ValidationError

from app.realtime.events import (
    ENVELOPE_RESERVED_FIELDS,
    EVENT_SPECS,
    EventType,
    NotificationEvent,
    TaskAssignedPayload,
    UnknownEventTypeError,
    all_event_types,
    build_payload,
    get_spec,
    render_title,
)

# 本文件混有同步与异步用例，故**不设全局 asyncio 标记**——
# pyproject 里 asyncio_mode = "auto"，异步函数会被自动识别；
# 给同步函数套 asyncio 标记只会产生 warning。


# ----------------------------------------------------------------------
# 结构性自检
# ----------------------------------------------------------------------
def test_every_event_type_is_registered():
    """EventType 的每个成员都必须在 EVENT_SPECS 登记。"""
    missing = [t for t in EventType if t not in EVENT_SPECS]
    assert not missing, f"以下事件类型没有在 EVENT_SPECS 登记：{missing}"


def test_spec_type_matches_its_key():
    """注册表的 key 与 spec.event_type 必须一致（手写注册表容易漏改一处）。

    注：字段名是 `event_type` 而不是 `type`——后者会在类体内遮蔽内建 `type`，
    导致同类的 `payload_model: type[_Payload]` 注解被 mypy 解析成该字段。
    """
    for key, spec in EVENT_SPECS.items():
        assert spec.event_type == key, f"{key} 的 spec.event_type 是 {spec.event_type}"


@pytest.mark.parametrize("event_type", list(EventType), ids=lambda t: str(t))
def test_title_template_only_uses_payload_fields(event_type: EventType):
    """标题模板的占位符必须都是该 payload 的字段——拼错一个字母就会被这条抓住。"""
    spec = EVENT_SPECS[event_type]
    fields = set(spec.payload_model.model_fields)
    placeholders = {name for _, name, _, _ in string.Formatter().parse(spec.title_template) if name}
    unknown = placeholders - fields
    assert not unknown, (
        f"{event_type} 的标题模板引用了 payload 里不存在的字段 {unknown}；"
        f"可用字段：{sorted(fields)}"
    )


@pytest.mark.parametrize("event_type", list(EventType), ids=lambda t: str(t))
def test_event_type_value_fits_column(event_type: EventType):
    """`notifications.type` 是 String(32)，取值不能超长（超了会截断/报错）。"""
    assert len(str(event_type)) <= 32, f"{event_type} 超过 32 字符"


def test_all_event_types_returns_registry_keys():
    assert set(all_event_types()) == set(EVENT_SPECS)


@pytest.mark.parametrize("event_type", list(EventType), ids=lambda t: str(t))
def test_payload_fields_do_not_collide_with_envelope(event_type: EventType):
    """payload 字段名不得与信封字段重名。

    ★ 这是踩过一次的坑：`MemberJoinedPayload` 原本带 `tenant_id`，而
      `NotificationEvent.create(tenant_id=...)` 的关键字会被**信封**先吃掉，
      payload 侧就永远取不到值，报的是「Field required」——
      看报错完全想不到是「重名」，会往「忘记传参」的方向排查。
      所以用一条结构性测试把它钉死，而不是靠注释提醒。
    """
    spec = EVENT_SPECS[event_type]
    collisions = set(spec.payload_model.model_fields) & ENVELOPE_RESERVED_FIELDS
    assert not collisions, (
        f"{event_type} 的 payload 字段 {collisions} 与信封字段重名；"
        f"信封已占用：{sorted(ENVELOPE_RESERVED_FIELDS)}"
    )


def test_envelope_reserved_fields_cover_create_signature():
    """保留字段清单必须覆盖 `NotificationEvent.create` 的真实关键字。"""
    import inspect

    params = set(inspect.signature(NotificationEvent.create).parameters) - {"cls", "kwargs"}
    assert params <= ENVELOPE_RESERVED_FIELDS, (
        f"create() 有 {params - ENVELOPE_RESERVED_FIELDS} 未被列入保留字段"
    )


# ----------------------------------------------------------------------
# payload 校验
# ----------------------------------------------------------------------
def test_build_payload_validates_and_serializes():
    payload = build_payload(
        EventType.TASK_ASSIGNED,
        task_id=1,
        task_title="实现事件系统",
        project_id=2,
        project_name="协作平台",
        assigned_by=9,
    )
    assert payload["task_id"] == 1
    assert payload["task_title"] == "实现事件系统"
    assert payload["assigned_by"] == 9
    # 未传的字段取模型的默认值，而不是缺失
    assert payload["reassigned"] is False


def test_build_payload_rejects_unknown_field():
    """extra=forbid：payload 会进数据库，必须是封闭结构。"""
    with pytest.raises(ValidationError):
        build_payload(
            EventType.TASK_ASSIGNED,
            task_id=1,
            task_title="t",
            project_id=2,
            project_name="p",
            sneaky_field="不该被塞进来",
        )


def test_build_payload_rejects_missing_required_field():
    with pytest.raises(ValidationError):
        build_payload(EventType.TASK_ASSIGNED, task_id=1)  # 缺 task_title 等


def test_build_payload_rejects_unregistered_type():
    with pytest.raises(UnknownEventTypeError):
        build_payload("task.not_a_real_event", task_id=1)


def test_get_spec_accepts_string_and_enum():
    assert get_spec(EventType.TASK_ASSIGNED).event_type is EventType.TASK_ASSIGNED
    assert get_spec("task.assigned").event_type is EventType.TASK_ASSIGNED


def test_get_spec_raises_on_unknown():
    with pytest.raises(UnknownEventTypeError):
        get_spec("nope.nope")


def test_payload_models_forbid_extra():
    """所有 payload 模型都继承带 extra=forbid 的基类。"""
    for spec in EVENT_SPECS.values():
        assert spec.payload_model.model_config.get("extra") == "forbid", (
            f"{spec.type} 的 payload 模型没有禁止额外字段"
        )
    assert TaskAssignedPayload.model_config.get("extra") == "forbid"


# ----------------------------------------------------------------------
# 标题渲染
# ----------------------------------------------------------------------
def test_render_title_fills_real_values():
    payload = build_payload(
        EventType.TASK_STATUS_CHANGED,
        task_id=1,
        task_title="实现事件系统",
        from_status="TODO",
        to_status="IN_PROGRESS",
    )
    title = render_title(str(EventType.TASK_STATUS_CHANGED), payload)
    assert "实现事件系统" in title
    assert "TODO" in title and "IN_PROGRESS" in title


def test_render_title_falls_back_on_unknown_type():
    """历史行里的旧类型不该让通知列表 500。"""
    assert render_title("legacy.removed_event", {}) == "有一条新通知"


def test_render_title_falls_back_on_payload_mismatch():
    """payload 结构演进过（缺字段）时同样降级，而不是抛错。"""
    assert render_title(str(EventType.TASK_ASSIGNED), {"task_id": 1}) == "有一条新通知"


# ----------------------------------------------------------------------
# NotificationEvent
# ----------------------------------------------------------------------
def test_notification_event_create_validates_payload():
    evt = NotificationEvent.create(
        event_type=EventType.MEMBER_JOINED,
        tenant_id=7,
        target_user_id=42,
        tenant_name="Acme",
        role_codes=["MEMBER"],
    )
    assert evt.type is EventType.MEMBER_JOINED
    assert evt.tenant_id == 7
    assert evt.target_user_id == 42
    assert evt.actor_id is None
    assert evt.payload["tenant_name"] == "Acme"


def test_notification_event_create_rejects_bad_payload():
    with pytest.raises(ValidationError):
        NotificationEvent.create(
            event_type=EventType.MEMBER_JOINED,
            tenant_id=1,
            target_user_id=2,
            # 缺 tenant_name
        )


def test_notification_event_to_message_shape():
    """推送消息体的字段与通知列表保持一致，前端只需一套渲染逻辑。"""
    evt = NotificationEvent.create(
        event_type=EventType.TASK_MENTIONED,
        tenant_id=1,
        target_user_id=5,
        actor_id=9,
        task_id=3,
        task_title="任务标题",
        comment_id=11,
        excerpt="…@你 看一下…",
        mentioned_by=9,
    )
    msg = evt.to_message()
    assert msg["event"] == "task.mentioned"
    assert msg["tenant_id"] == 1
    assert msg["user_id"] == 5
    assert msg["actor_id"] == 9
    assert "任务标题" in msg["title"]
    assert msg["payload"]["comment_id"] == 11
    assert isinstance(msg["created_at"], str)


def test_notification_event_create_normalizes_string_type():
    """传字符串类型也要落到枚举上，保证入库值与枚举一致。"""
    evt = NotificationEvent.create(
        event_type="role.assigned",
        tenant_id=1,
        target_user_id=2,
        role_id=3,
        role_code="MEMBER",
        role_name="普通成员",
        data_scope="SELF",
    )
    assert evt.type is EventType.ROLE_ASSIGNED
    assert str(evt.type) == "role.assigned"
