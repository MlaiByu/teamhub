"""邮件异步发送任务。

★ 为什么邮件必须异步（这是方案 62 里「邮件」被列进 Celery 的原因）：

  SMTP 投递是**网络 IO**，耗时在几百毫秒到数秒之间，且对端可能限流/抖动。
  放在请求链路里同步发，用户注册或拉人入团要等好几秒才返回——
  而这期间业务数据其实早就写好了。异步化后接口立刻返回，邮件在后台发。

★ 重试策略：只重试**可恢复**的异常（网络 / SMTP 协议错误）。

  `autoretry_for=(Exception,)` 看起来省事，但它会把「代码写错了」这类
  永远不会成功的错误也重试三遍——白等三倍时间，还掩盖真问题。
  所以只列 OSError（网络）与 smtplib.SMTPException（协议/认证）。
"""

from __future__ import annotations

import smtplib

from app.core.logging import get_logger
from app.core.mailer import get_mailer
from app.tasks.celery_app import celery_app

logger = get_logger(__name__)


@celery_app.task(
    name="app.tasks.email.send_email",
    autoretry_for=(OSError, smtplib.SMTPException),
    retry_backoff=True,
    retry_backoff_max=300,
    max_retries=3,
)
def send_email(
    *, to: str, subject: str, body: str, tenant_id: int | None = None
) -> dict[str, str | None]:
    """发送一封邮件。

    ★ `tenant_id` 是**可选**的，且本任务不带 `@tenant_task`：

      邮件的主题与正文里不含任何租户业务数据（调用方把内容拼好传进来），
      任务本身也不查库，所以严格说它不需要租户上下文——
      强行套 `@tenant_task` 反而会让「发纯通知邮件」也必须传租户。

      保留这个参数是为了：① 日志按租户归集便于排查；
      ② 将来做「按租户配置发信人 / 邮件模板」时不用改签名。

      **判断依据**：一个任务是否需要 `@tenant_task`，取决于它**是否碰租户级表**。
      碰 → 必须（否则钩子不过滤，跨租户）；不碰 → 可以不套。
    """
    get_mailer().send(to=to, subject=subject, body=body)
    logger.info("email_sent", to=to, subject=subject, tenant_id=tenant_id)
    return {"to": to, "subject": subject}
