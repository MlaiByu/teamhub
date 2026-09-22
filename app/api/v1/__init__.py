"""v1 路由汇总。

各领域路由随阶段推进逐步接入（见 PROJECT-PLAN 十二）。
第 2 周接入认证；租户 / 组织 / RBAC / 项目任务随后。

顺序按「路径前缀」分组，便于读 OpenAPI 时定位。
"""

from fastapi import APIRouter

from app.api.v1 import (
    attachments,
    audit,
    auth,
    comments,
    departments,
    members,
    notifications,
    projects,
    roles,
    system,
    tasks,
    tenants,
    ws,
)

api_v1_router = APIRouter()
api_v1_router.include_router(system.router)
api_v1_router.include_router(attachments.router)
api_v1_router.include_router(audit.router)
api_v1_router.include_router(auth.router)
api_v1_router.include_router(tenants.router)
api_v1_router.include_router(departments.router)
api_v1_router.include_router(members.router)
api_v1_router.include_router(roles.router)
api_v1_router.include_router(projects.router)
api_v1_router.include_router(tasks.router)
api_v1_router.include_router(comments.router)
api_v1_router.include_router(notifications.router)
api_v1_router.include_router(ws.router)
