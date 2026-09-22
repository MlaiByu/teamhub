"""任务评论 schema。

★ 提及（mention）**不接受客户端传 id 列表**，只从正文里解析。
  理由：如果客户端能直接指定「通知谁」，任何成员都能给全公司发任意通知
  ——通知系统就成了骚扰通道。解析服务端做，且只认当前租户的 ACTIVE 成员，
  这是权限边界而不是实现细节。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CommentCreateRequest(BaseModel):
    """发表评论。正文里可用 `@用户名` 提及成员。"""

    content: str = Field(
        min_length=1,
        max_length=4000,
        examples=["@张三 这个接口明天能联调吗？"],
        description="评论正文；`@用户名` 会被解析并给被提及者发通知",
    )


class CommentOut(BaseModel):
    """一条评论。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    author_id: int
    content: str
    created_at: datetime
