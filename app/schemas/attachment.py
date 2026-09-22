"""附件 schema。

★ 附件对外的字段里**没有** `path`：
  `path` 是存储后端（本地磁盘 / MinIO）的内部路径，一旦回给客户端，
  换后端、迁移存储位置都会破坏旧链接；而且泄露了服务端文件布局。
  客户端只拿附件 id，通过下载接口取回字节。

★ 也没有「原始文件名」作为权威字段回显给下载：下载时的 `filename` 由
  服务端按「安全前缀 + 扩展名」重新生成。原因：原始文件名是**用户输入**，
  直接拿它当下载文件名，等于允许注入任意字符（换行、引号、非 ASCII），
  可能被用于 Content-Disposition 头注入。所以上传时只保留扩展名做类型判断。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AttachmentOut(BaseModel):
    """附件元数据。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    biz_type: str
    biz_id: int
    size: int
    created_at: datetime


class AttachmentDownloadInfo(BaseModel):
    """下载接口返回的元信息（不含字节；字节走 StreamingResponse）。"""

    filename: str
    size: int
