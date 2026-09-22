"""用户仓储。

★ `User` 是**全局表**（同一账号可加入多个租户，见 PROJECT-PLAN 7.2），
  所以继承 `BaseRepository` 而非 `TenantAwareRepository`。

  这不是省事，是必须：登录发生在「还没有租户上下文」的时刻——
  上下文要靠 access token 才能设，而 token 正是登录要签发的产物。
  给这条查询套租户守卫，登录会永远失败。
"""

from __future__ import annotations

from app.models import User
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_username(self, username: str) -> User | None:
        """按用户名查用户。登录路径的入口查询。"""
        return await self.find_one_by(username=username)

    async def get_by_email(self, email: str) -> User | None:
        return await self.find_one_by(email=email)

    async def username_taken(self, username: str) -> bool:
        """注册时的唯一性预检。

        ★ 这只是「提前给出友好报错」，**不能**当作唯一性的保证——
          并发注册仍可能同时通过预检。真正的保证来自
          `users.username` 的 UNIQUE 约束，注册时要 catch IntegrityError。
        """
        return await self.get_by_username(username) is not None
