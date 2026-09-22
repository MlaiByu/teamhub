"""模型统一再导出。业务侧写 `from app.models import Task`，不关心文件切分。

★ 这个 __init__ 还有一个不显眼但关键的作用：
    它保证所有模型在 Base.metadata 里注册完成。
    Alembic autogenerate 与 create_all 都依赖「模型已导入」。
    漏掉任何一个模块，迁移就会漏表——而且不报错，只是安静地少建一张。
"""

from app.models.misc import Attachment, AuditLog, Notification
from app.models.org import Department
from app.models.project import Project
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.task import Task, TaskComment
from app.models.tenant import Tenant, TenantMember
from app.models.user import RefreshToken, User

__all__ = [
    # 租户（隔离的根）
    "Tenant",
    "TenantMember",
    # 认证
    "User",
    "RefreshToken",
    # 组织
    "Department",
    # RBAC
    "Permission",
    "Role",
    "RolePermission",
    "UserRole",
    # 项目 / 任务
    "Project",
    "Task",
    "TaskComment",
    # 附件 / 通知 / 审计
    "Attachment",
    "Notification",
    "AuditLog",
]
