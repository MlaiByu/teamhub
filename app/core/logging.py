"""structlog 配置。每个请求一个 request_id，贯穿日志与错误响应（11.4）。"""

from __future__ import annotations

import logging
import sys
import uuid
from contextvars import ContextVar

import structlog
from structlog.typing import EventDict, WrappedLogger

# 请求 ID 存 contextvar，日志 processor 自动带上。
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def _add_request_id(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    rid = request_id_var.get()
    if rid:
        event_dict["request_id"] = rid
    return event_dict


def configure_logging(level: str = "INFO", *, json_output: bool = False) -> None:
    """开发环境用彩色 console，生产用 JSON（便于采集）。"""
    renderer = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=False),
            _add_request_id,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
