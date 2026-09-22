"""WebSocket 连接管理。

★ 连接按 **(tenant_id, user_id)** 索引 —— 这是 PROJECT-PLAN 风险 8 的落地点。

  如果只按 user_id 索引：本项目的 `User` 是**全局表**（同一账号可加入多个
  租户），同一个 user_id 在租户 A 和租户 B 各有一条连接时，两者会落进
  同一个桶。推给「租户 A 的张三」的通知就会送到「租户 B 的张三」的 socket 上
  —— 跨租户泄露，而且**不会报错**，只是张三某天在 B 公司的界面上看到了
  A 公司的任务标题。

  所以：键必须带租户，且推送函数**强制要求显式传 tenant_id**
  （而不是从消息里推断），让「忘了传租户」变成签名层面的编译错误。

★ 连接集合用 `set` 而不是单个对象：同一用户可能同时开多个标签页 / 设备。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, Protocol

from app.core.logging import get_logger

logger = get_logger(__name__)


class Sendable(Protocol):
    """连接需要满足的最小接口。

    ★ 定义 Protocol 而不是直接依赖 starlette 的 `WebSocket`：
      这样测试可以塞一个假连接（只记下收到了什么），不需要起真的 ASGI 服务；
      也让本模块不必 import Web 框架——`realtime/` 只依赖 core 是更干净的方向。
    """

    async def send_json(self, data: Any) -> None: ...


# 连接键：租户 + 用户。两者共同构成唯一定位。
ConnectionKey = tuple[int, int]


class ConnectionManager:
    """按 (tenant_id, user_id) 维护 WebSocket 连接并投递消息。"""

    def __init__(self) -> None:
        self._connections: dict[ConnectionKey, set[Sendable]] = defaultdict(set)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 连接生命周期
    # ------------------------------------------------------------------
    async def connect(self, *, tenant_id: int, user_id: int, websocket: Sendable) -> None:
        async with self._lock:
            self._connections[(tenant_id, user_id)].add(websocket)
        logger.info(
            "ws_connected", tenant_id=tenant_id, user_id=user_id, total=self.connection_count()
        )

    async def disconnect(self, *, tenant_id: int, user_id: int, websocket: Sendable) -> None:
        async with self._lock:
            key = (tenant_id, user_id)
            bucket = self._connections.get(key)
            if bucket is not None:
                bucket.discard(websocket)
                # 空桶必须删掉，否则长期运行会积累大量空 key（内存泄漏）
                if not bucket:
                    self._connections.pop(key, None)
        logger.info(
            "ws_disconnected", tenant_id=tenant_id, user_id=user_id, total=self.connection_count()
        )

    # ------------------------------------------------------------------
    # 投递
    # ------------------------------------------------------------------
    async def send_to_user(self, *, tenant_id: int, user_id: int, message: dict) -> int:
        """推给某租户下某用户的全部连接。返回成功送达数。

        ★ `tenant_id` 是必填关键字参数，不是可选的——这是刻意的：
          让它无法被「忘了传」而静默推给所有同名 user_id 的连接。
        """
        async with self._lock:
            targets = list(self._connections.get((tenant_id, user_id), ()))

        return await self._deliver(targets, message, tenant_id=tenant_id, user_id=user_id)

    async def send_to_tenant(self, *, tenant_id: int, message: dict) -> int:
        """推给某租户下的**全部**在线连接。用于租户级广播（如公告）。

        实现上按 key 过滤而不是遍历所有连接——键的第一位就是 tenant_id。
        """
        async with self._lock:
            targets: list[Sendable] = []
            for key, bucket in self._connections.items():
                if key[0] == tenant_id:
                    targets.extend(bucket)

        return await self._deliver(targets, message, tenant_id=tenant_id, user_id=None)

    async def _deliver(
        self,
        targets: list[Sendable],
        message: dict,
        *,
        tenant_id: int,
        user_id: int | None,
    ) -> int:
        """逐个投递。

        ★ 单个连接发送失败**不能**中断其余投递：一个卡死的客户端
          不该让整个租户的通知都发不出去。失败的连接顺手清理掉。
        """
        if not targets:
            return 0

        delivered = 0
        dead: list[Sendable] = []
        for conn in targets:
            try:
                await conn.send_json(message)
                delivered += 1
            except Exception:  # noqa: BLE001 — 任何异常都只意味着这条连接不能用了
                dead.append(conn)
                # ★ 关键字不能用 `event`：structlog 的第一个位置参数就叫 event，
                #   传 `event=...` 会撞成「同一参数收到两个值」而抛 TypeError，
                #   把「某条连接发送失败」升级成「整个投递函数崩掉」。
                logger.warning(
                    "ws_deliver_failed",
                    tenant_id=tenant_id,
                    user_id=user_id,
                    event_type=message.get("event"),
                )

        if dead:
            async with self._lock:
                for key, bucket in list(self._connections.items()):
                    for conn in dead:
                        bucket.discard(conn)
                    if not bucket:
                        self._connections.pop(key, None)

        return delivered

    # ------------------------------------------------------------------
    # 自检 / 测试辅助
    # ------------------------------------------------------------------
    def connection_count(self, *, tenant_id: int | None = None) -> int:
        """连接数。传 tenant_id 则只统计该租户。"""
        if tenant_id is None:
            return sum(len(bucket) for bucket in self._connections.values())
        return sum(len(bucket) for key, bucket in self._connections.items() if key[0] == tenant_id)

    def connected_keys(self) -> set[ConnectionKey]:
        """当前有连接的 (tenant_id, user_id) 集合。测试用。"""
        return {key for key, bucket in self._connections.items() if bucket}

    async def close_all(self) -> None:
        """清空全部连接。用于应用关停与测试清理。"""
        async with self._lock:
            self._connections.clear()
        logger.info("ws_all_closed")


# 进程内单例。WS 端点在路由器里直接用它；
# 测试要隔离时自己 new 一个 ConnectionManager()。
manager = ConnectionManager()
