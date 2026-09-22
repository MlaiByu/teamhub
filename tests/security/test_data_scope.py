"""数据范围（越权）测试骨架。

第 1 周只建骨架 + 已可验证的部分（SELF/DEPT/ALL 的过滤行为在
tests/security/test_tenant_isolation.py 里已覆盖）。
第 6 周补齐完整的「角色 × 接口 × 范围」矩阵。

★ 越权测试与隔离测试的分工：
    隔离 = 能不能跨租户（横向）
    越权 = 同租户内能不能看到不该看的数据（纵向）
两者都是 tests/security/ 的职责，不混进业务测试。
"""

from __future__ import annotations

import pytest

from app.core.constants import DataScope, widest_scope

# 注意：不要把 pytestmark 设成 asyncio——本文件里有同步用例。
# 需要 async 的用例单独加 @pytest.mark.asyncio。


def test_widest_scope_picks_broadest():
    """多角色取最宽（5.1）。

    这条收敛在 core/constants.widest_scope 一处计算，
    就是为了避免散在多个 service 里各算一遍、早晚算错。
    """
    assert widest_scope([DataScope.SELF, DataScope.DEPT]) == DataScope.DEPT
    assert widest_scope([DataScope.SELF, DataScope.ALL]) == DataScope.ALL
    assert widest_scope([DataScope.DEPT, DataScope.ALL]) == DataScope.ALL
    assert widest_scope([]) == DataScope.SELF  # 无角色 → 最小权限


@pytest.mark.asyncio
@pytest.mark.skip(reason="第 6 周实现：角色 × 接口 × 数据范围的允许/拒绝矩阵")
async def test_scope_matrix_placeholder():
    """占位：确保第 9 周「测试冻结」时不会漏掉这块。"""
    raise AssertionError("未实现")
