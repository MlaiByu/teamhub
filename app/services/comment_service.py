"""任务评论服务：发表评论、解析 @提及、触发生成事件。

★ 提及解析为什么放在服务端、且只认本租户成员：

  如果让客户端直接传「要通知谁」，任何成员都能给全公司发任意通知——
  通知系统会变成骚扰通道。所以：
    1. 只从**正文**解析（客户端只能通过「写出 @名字」来提及）
    2. 解析出的名字必须能在**当前租户的 ACTIVE 成员**里找到
       （`TenantMemberRepository.resolve_usernames`，一次查询解析一批）
    3. 解析不到的名字**静默跳过**——@ 一个不存在的人不该让评论发不出去

★ 评论的可见性继承任务：所有读写都先 `TaskRepository.get_or_404(task_id)`，
  让任务自己的「租户 + 数据范围」双重过滤决定可见性。评论表不带
  DataScopedMixin，不需要（也不应该）复制一套 owner_id/dept_id 逻辑。
"""

from __future__ import annotations

import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import TaskComment
from app.realtime.events import EventType
from app.repositories.comment import TaskCommentRepository
from app.repositories.task import TaskRepository
from app.repositories.tenant import TenantMemberRepository
from app.services import notification_service

logger = get_logger(__name__)

# ----------------------------------------------------------------------
# 提及解析
# ----------------------------------------------------------------------
# 允许出现的字符：Unicode 字母（含中文）/ 数字 / 下划线 / 点 / 连字符。
# `\w` 在 str 模式下默认是 Unicode 语义，所以中文、日文等都能命中。
#
# ★ 为什么用「白名单」而不是「@ 之后取到分隔符为止」的黑名单写法：
#   黑名单要把所有可能的标点列全，漏一个就会把标点吃进名字里
#   （`@张三，你好` → 名字变成「张三，你好」）；而白名单只需要覆盖
#   用户名的合法字符，多出来的标点自然截断。
#
# ★ 为什么**不加** `(?<![\w])` 这类后顾断言来排除邮箱：
#   那会连带排除 `@张三@李四` 这种连写提及（第二个 `@` 前面正是汉字），
#   造成**静默漏发通知**。而邮箱带来的误判（`me@example.com` → 候选名
#   `example.com`）是完全无害的：它不可能匹配到任何真实成员，
#   `resolve_usernames` 查不到就跳过。
#   **漏发一条真实通知，比多解析一个解析不到的名字严重得多。**
_MENTION_PATTERN = re.compile(r"@([\w.\-]+)")

# 通知摘要长度上限。太长会撑爆通知列表的一行。
_EXCERPT_LIMIT = 60


def extract_mentions(content: str) -> list[str]:
    """从正文提取被 @ 的用户名，**去重且保持出现顺序**。

    只做「像不像用户名」的形态判断；**是否真的能通知到**交给
    `resolve_usernames` 按租户成员关系裁决。
    """
    seen: set[str] = set()
    names: list[str] = []
    for match in _MENTION_PATTERN.finditer(content):
        name = match.group(1)
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _excerpt(content: str, limit: int = _EXCERPT_LIMIT) -> str:
    """把正文压成一行摘要：换行折成空格，超长截断加省略号。"""
    flat = " ".join(content.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


# ----------------------------------------------------------------------
# 发表评论
# ----------------------------------------------------------------------
async def create_comment(
    session: AsyncSession, *, task_id: int, content: str, author_id: int
) -> TaskComment:
    """发表评论，并触发评论 / 提及事件。

    时序：**业务变更（评论落库）先提交，再 emit 通知**——
    `notification_service.emit` 要求如此（见其模块 docstring）。
    """
    # 可见性校验：跨租户 / 超出数据范围都 404
    task = await TaskRepository(session).get_or_404(task_id)

    comment = TaskCommentRepository(session).create(
        task_id=task_id, author_id=author_id, content=content
    )
    await session.flush()

    # 先把要用的值取出来：emit 会 commit，虽然 expire_on_commit=False
    # 让属性仍可读，但提前取值能让「通知内容」与「落库内容」的对应关系一眼可见。
    task_title = task.title
    task_assignee_id = task.assignee_id
    comment_id = comment.id

    await session.commit()

    await _emit_comment_events(
        session,
        task_id=task_id,
        task_title=task_title,
        task_assignee_id=task_assignee_id,
        comment_id=comment_id,
        content=content,
        author_id=author_id,
    )

    logger.info("comment_created", comment_id=comment_id, task_id=task_id, author_id=author_id)
    return comment


async def _emit_comment_events(
    session: AsyncSession,
    *,
    task_id: int,
    task_title: str,
    task_assignee_id: int | None,
    comment_id: int,
    content: str,
    author_id: int,
) -> None:
    """按「提及优先于评论」的规则投递通知。

    ★ 为什么被 @ 的人不再额外收一条「任务有新评论」：
      一次操作产生两条通知是噪音。@ 是**更具体**的信号
      （它说明「这条评论是冲你来的」），所以提及命中时就不再发评论通知。
      执行人若同时被 @，只收提及那一条。
    """
    excerpt = _excerpt(content)
    mentioned_ids = await _resolve_mentions(session, content)

    if mentioned_ids:
        await notification_service.emit_to_many(
            session,
            event_type=EventType.TASK_MENTIONED,
            target_user_ids=mentioned_ids,
            actor_id=author_id,
            task_id=task_id,
            task_title=task_title,
            comment_id=comment_id,
            excerpt=excerpt,
            mentioned_by=author_id,
        )

    # 执行人：跳过作者自己，也跳过已被 @ 的人（避免重复通知）
    if (
        task_assignee_id is not None
        and task_assignee_id != author_id
        and task_assignee_id not in mentioned_ids
    ):
        await notification_service.emit(
            session,
            event_type=EventType.TASK_COMMENTED,
            target_user_id=task_assignee_id,
            actor_id=author_id,
            task_id=task_id,
            task_title=task_title,
            comment_id=comment_id,
            excerpt=excerpt,
            author_id=author_id,
        )


async def _resolve_mentions(session: AsyncSession, content: str) -> list[int]:
    """解析正文里的 @，返回**本租户 ACTIVE 成员**的 user_id 列表（去重保序）。"""
    names = extract_mentions(content)
    if not names:
        return []

    resolved = await TenantMemberRepository(session).resolve_usernames(names)

    unresolved = [n for n in names if n not in resolved]
    if unresolved:
        # 只记日志不报错：@ 拼错、@ 了非成员都是常见情况，不该让评论失败。
        logger.info("mentions_unresolved", names=unresolved)

    # dict.fromkeys 去重保序：两个不同名字可能解析到同一个 user_id（不会发生，
    # username 全局唯一，但保持这个习惯——去重逻辑不依赖上游的唯一性假设）
    return list(dict.fromkeys(resolved.values()))


# ----------------------------------------------------------------------
# 读取
# ----------------------------------------------------------------------
async def list_comments(session: AsyncSession, *, task_id: int) -> list[TaskComment]:
    """列出任务的评论，时间正序。

    ★ 先取任务（受租户 + 数据范围过滤），再取评论——
      这是「评论可见性继承任务」的落地点。少了这一步，
      任何一个知道 task_id 的成员都能读到别人任务的讨论。
    """
    await TaskRepository(session).get_or_404(task_id)
    return list(await TaskCommentRepository(session).list_for_task(task_id))
