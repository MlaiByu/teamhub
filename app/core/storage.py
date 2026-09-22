"""本地文件存储。

★ 为什么是一个可替换的抽象，而不是直接把 `open()` 写死在 service 里：

  方案 112 明确「附件上传先本地存储，MinIO 作为可替换实现」。如果附件 service
  直接依赖文件系统，将来换成 MinIO（或 S3）时得改所有读写点。抽一个极简的
  `StorageBackend` 协议（save / open / delete / exists），service 只面向协议编程，
  换后端 = 换一个实现类，业务代码零改动。

★ 为什么文件**不直接**进 `attachments` 表，而是「表存元数据 + 磁盘存字节」：

  附件动辄几 MB，塞进关系库会让库体膨胀、备份变慢、查询拖垮。
  表里只存 `path`（相对后端根的路径），字节落在磁盘（本地）或对象存储（MinIO）。
  所以 `path` 是「后端内部路径」，**绝不回给客户端**——客户端只拿到附件 id，
  通过下载接口取回，这样后端才能自由切换存储位置。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Protocol

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class StorageBackend(Protocol):
    """附件存储的最小接口。换 MinIO/S3 时实现同名协议即可。"""

    async def save(self, *, tenant_id: int, data: bytes, ext: str) -> str:
        """保存字节，返回**后端内部相对路径**（存进 attachments.path）。"""
        ...

    async def open(self, path: str) -> bytes:
        """按路径读回字节。"""
        ...

    async def delete(self, path: str) -> None:
        """按路径删除。"""
        ...

    async def exists(self, path: str) -> bool: ...


class LocalStorage:
    """本地磁盘存储。文件落在 `<root>/<tenant_id>/<uuid>.<ext>`。

    ★ 路径隔离：每个租户一个子目录，文件系统层面就分开了——
      即便将来要按租户打包归档、单独清理或设配额，也能直接对目录操作。
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self._root = Path(root) if root is not None else Path(settings.storage_dir)

    def _resolve(self, path: str) -> Path:
        """把内部相对路径解析为绝对路径，并**强制它落在存储根内**。

        ★ 为什么不能直接用 `self._root / path`：
          `path` 来自数据库，而数据库字段原则上是「应用自己写的可信数据」，
          但审计/修复脚本、历史脏数据、或未来某个写坏 path 的 bug 都可能
          让 `path` 变成 `../../etc/passwd` 这种穿越串。resolve 后校验
          是否仍在 root 下，等于给文件系统访问上了一道「路径穿越」防线。
          这条不是优化，是安全必需——下载接口会拿数据库的 path 去读文件。
        """
        full = (self._root / path).resolve()
        if not full.is_relative_to(self._root.resolve()):
            raise ValueError(f"非法附件路径（路径穿越）：{path}")
        return full

    async def save(self, *, tenant_id: int, data: bytes, ext: str) -> str:
        rel = f"{tenant_id}/{uuid.uuid4().hex}.{ext}"
        full = self._resolve(rel)
        full.parent.mkdir(parents=True, exist_ok=True)
        # 同步写入在 async 函数里会短暂阻塞事件循环；附件通常不大，
        # 且本地降级模式下追求的是「可运行」而非「极致吞吐」。
        # 真正的大文件/并发场景应换 MinIO 后端或用线程池。
        full.write_bytes(data)
        return rel

    async def open(self, path: str) -> bytes:
        full = self._resolve(path)
        if not full.is_file():
            raise FileNotFoundError(path)
        return full.read_bytes()

    async def delete(self, path: str) -> None:
        full = self._resolve(path)
        try:
            full.unlink(missing_ok=True)
        except OSError:
            logger.warning("attachment_delete_failed", path=path)

    async def exists(self, path: str) -> bool:
        try:
            return self._resolve(path).is_file()
        except ValueError:
            return False


# 进程内默认实例（测试可注入 tmp 目录的独立实例）
storage: LocalStorage = LocalStorage()


# 路径生成兜底：万一 storage 是别家实现，仍然要 ext 安全
def safe_ext(filename: str) -> str:
    """从原始文件名提取扩展名，小写、去点。"""
    _, dot, ext = filename.rpartition(".")
    return ext.lower() if dot and ext else ""


def random_key() -> str:
    return uuid.uuid4().hex
