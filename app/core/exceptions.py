"""应用异常与全局异常处理器。

约定：业务层只抛 AppException 子类，HTTP 状态码由 BizCode 统一映射，
不在 service 里 import fastapi（保持 core/业务层不依赖 Web 框架）。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.constants import BizCode
from app.core.logging import get_logger
from app.core.responses import fail

logger = get_logger(__name__)


class AppException(Exception):
    """所有业务异常的基类。"""

    code: int = BizCode.INTERNAL_ERROR
    message: str = "服务器内部错误"

    def __init__(self, message: str | None = None, *, code: int | None = None, data=None):
        self.message = message or self.message
        self.code = code or self.code
        self.data = data
        super().__init__(self.message)

    @property
    def http_status(self) -> int:
        return BizCode.http_status(self.code)


class ParamInvalidError(AppException):
    code = BizCode.PARAM_INVALID
    message = "参数校验失败"


class UnauthenticatedError(AppException):
    code = BizCode.UNAUTHENTICATED
    message = "未认证或令牌已过期"


class ForbiddenError(AppException):
    """无权限（角色或数据范围不足）。

    注意：跨租户访问**不用**这个，要用 NotFoundError——
    否则等于承认「这个 ID 存在，只是你没权限」，造成存在性泄露（PLAN 十二·第3条）。
    """

    code = BizCode.FORBIDDEN
    message = "无权限执行该操作"


class NotFoundError(AppException):
    code = BizCode.NOT_FOUND
    message = "资源不存在"


class ConflictError(AppException):
    code = BizCode.CONFLICT
    message = "业务冲突"


class RateLimitedError(AppException):
    code = BizCode.RATE_LIMITED
    message = "请求过于频繁，请稍后再试"


class TenantContextMissingError(RuntimeError):
    """租户上下文未设置（坑 1）。

    继承 RuntimeError 而非 AppException：这是**编程错误**，不是业务错误，
    不该被业务代码 catch 掉。它必须让开发者立刻看到。
    """


def _envelope(code: int, message: str, data=None) -> dict:
    """与中间件共用同一套信封形状（见 responses.fail 的说明）。"""
    return fail(code, message, data)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppException)
    async def _app_exc(_: Request, exc: AppException) -> JSONResponse:
        if exc.http_status >= 500:
            logger.error("app_exception", code=exc.code, message=exc.message, exc_info=exc)
        return JSONResponse(
            status_code=exc.http_status,
            content=_envelope(exc.code, exc.message, exc.data),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(_: Request, exc: RequestValidationError) -> JSONResponse:
        # 把 Pydantic 的报错压成可读字段路径，避免直接把内部结构吐给前端
        details = [
            {"field": ".".join(str(p) for p in e.get("loc", [])), "msg": e.get("msg", "")}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_envelope(BizCode.PARAM_INVALID, "参数校验失败", details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {401: BizCode.UNAUTHENTICATED, 403: BizCode.FORBIDDEN, 404: BizCode.NOT_FOUND}.get(
            exc.status_code, BizCode.INTERNAL_ERROR
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(code, str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_exception")
        return JSONResponse(
            status_code=500,
            content=_envelope(BizCode.INTERNAL_ERROR, "服务器内部错误"),
        )
