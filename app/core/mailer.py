"""邮件发送后端（可替换）。

★ 与 `core/storage.py` 同一个思路：定一个极简协议 + 两个实现，
  业务侧面向协议编程。换 SendGrid / SES 只需加一个实现类。

★ 为什么本地/测试用「写日志」而不是「抛错」：

  邮件是**旁路通知**——它失败不该让业务操作失败。
  若零依赖模式下直接 raise，本地任何触发邮件的路径（拉人入团、分配任务）
  都会报错，开发体验直接崩掉。
  写日志既保留了「邮件内容确实生成了」的可观测性（本地能看见发了什么），
  又不需要任何外部依赖。
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Protocol

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class Mailer(Protocol):
    """邮件后端的最小接口。"""

    def send(self, *, to: str, subject: str, body: str) -> None: ...


class ConsoleMailer:
    """把邮件内容写进日志。本地与测试的零依赖默认。"""

    def send(self, *, to: str, subject: str, body: str) -> None:
        logger.info("mail_console", to=to, subject=subject, body=body)


class SmtpMailer:
    """真实 SMTP 投递。"""

    def __init__(self) -> None:
        self._host = settings.smtp_host
        self._port = settings.smtp_port
        self._user = settings.smtp_user
        self._password = settings.smtp_password
        self._use_tls = settings.smtp_use_tls
        self._sender = settings.mail_from

    def send(self, *, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)

        # 超时必设：否则对端静默挂起会让 worker 线程一直卡着
        # （虽然有 task_time_limit 兜底，但那是整个任务级别，粒度太粗）
        with smtplib.SMTP(self._host, self._port, timeout=10) as smtp:
            if self._use_tls:
                smtp.starttls()
            if self._user:
                smtp.login(self._user, self._password or "")
            smtp.send_message(message)


def get_mailer() -> Mailer:
    """按配置选后端。

    ★ 每次调用都新建实例，不复用：SMTP 连接是有状态的，
      跨任务复用会在并发/重试场景下互相干扰。
    """
    if settings.mail_backend == "smtp":
        return SmtpMailer()
    return ConsoleMailer()
