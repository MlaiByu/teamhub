"""附件测试（第 5 周步骤 1）。

★ 覆盖三条安全边界：
  1. **归属校验**：附件只能挂到当前租户可见的实体上。跨租户的 biz_id → 404。
  2. **类型白名单**：.html/.svg 等可执行内容必须被拒（下载回浏览器会成 XSS 载体）。
  3. **大小上限**：超限不落盘。
  另有跨租户下载 404、下载文件名为服务端生成（防头注入）。
"""

from __future__ import annotations

import pytest

from app.core.storage import LocalStorage
from app.services import attachment_service

PASSWORD = "a-long-enough-passphrase"


@pytest.fixture
def tmp_storage(tmp_path, monkeypatch):
    """把存储后端换成 tmp 目录，避免测试污染真实 local_storage/。"""
    backend = LocalStorage(root=tmp_path / "storage")
    monkeypatch.setattr(attachment_service, "storage", backend)
    return backend


async def _register(client, username: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/register", json={"username": username, "password": PASSWORD}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _make_project(client, token: str, code: str = "P-1") -> int:
    resp = await client.post(
        "/api/v1/projects", json={"code": code, "name": f"项目{code}"}, headers=_h(token)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


# ----------------------------------------------------------------------
# 上传
# ----------------------------------------------------------------------
async def test_upload_and_list_and_download(client, tmp_storage):
    data = await _register(client, "guotao")
    token = data["token"]["access_token"]
    pid = await _make_project(client, token)

    resp = await client.post(
        "/api/v1/attachments",
        data={"biz_type": "project", "biz_id": str(pid)},
        files={"file": ("需求.md", "# 需求文档\n内容".encode(), "text/markdown")},
        headers=_h(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()["data"]
    assert body["biz_type"] == "project"
    assert body["biz_id"] == pid
    assert body["size"] == len("# 需求文档\n内容".encode())
    aid = body["id"]

    # 列表
    resp = await client.get(f"/api/v1/attachments?biz_type=project&biz_id={pid}", headers=_h(token))
    assert [a["id"] for a in resp.json()["data"]] == [aid]

    # 下载
    resp = await client.get(f"/api/v1/attachments/{aid}/download", headers=_h(token))
    assert resp.status_code == 200
    assert resp.content == "# 需求文档\n内容".encode()
    # 文件名是服务端生成的，不含原始名（防头注入）
    assert "attachment-" in resp.headers["content-disposition"]
    assert "需求.md" not in resp.headers["content-disposition"]


async def test_upload_rejects_disallowed_extension(client, tmp_storage):
    data = await _register(client, "guotao")
    token = data["token"]["access_token"]
    pid = await _make_project(client, token)

    resp = await client.post(
        "/api/v1/attachments",
        data={"biz_type": "project", "biz_id": str(pid)},
        files={"file": ("evil.html", b"<script>alert(1)</script>", "text/html")},
        headers=_h(token),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == 40000


async def test_upload_rejects_oversized_file(client, tmp_storage, monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.settings, "max_upload_bytes", 10)  # 压到 10 字节
    data = await _register(client, "guotao")
    token = data["token"]["access_token"]
    pid = await _make_project(client, token)

    resp = await client.post(
        "/api/v1/attachments",
        data={"biz_type": "project", "biz_id": str(pid)},
        files={"file": ("big.txt", b"x" * 100, "text/plain")},
        headers=_h(token),
    )
    assert resp.status_code == 409, resp.text


async def test_upload_to_cross_tenant_entity_returns_404(client, tmp_storage):
    """★ 附件不能挂到别的租户的实体上。"""
    alice = await _register(client, "alice")
    bob = await _register(client, "bob")
    alice_pid = await _make_project(client, alice["token"]["access_token"], "ALICE")

    resp = await client.post(
        "/api/v1/attachments",
        data={"biz_type": "project", "biz_id": str(alice_pid)},
        files={"file": ("x.txt", b"hi", "text/plain")},
        headers=_h(bob["token"]["access_token"]),
    )
    assert resp.status_code == 404, resp.text


async def test_upload_to_unknown_biz_type_rejected(client, tmp_storage):
    data = await _register(client, "guotao")
    token = data["token"]["access_token"]

    resp = await client.post(
        "/api/v1/attachments",
        data={"biz_type": "nonsense", "biz_id": "1"},
        files={"file": ("x.txt", b"hi", "text/plain")},
        headers=_h(token),
    )
    assert resp.status_code == 422


async def test_upload_requires_auth(client):
    resp = await client.post(
        "/api/v1/attachments",
        data={"biz_type": "project", "biz_id": "1"},
        files={"file": ("x.txt", b"hi", "text/plain")},
    )
    assert resp.status_code == 401


# ----------------------------------------------------------------------
# 下载的隔离
# ----------------------------------------------------------------------
async def test_download_cross_tenant_attachment_returns_404(client, tmp_storage):
    """★ 附件下载同样不能跨租户。"""
    alice = await _register(client, "alice")
    bob = await _register(client, "bob")
    alice_token = alice["token"]["access_token"]
    pid = await _make_project(client, alice_token, "ALICE-DL")

    up = await client.post(
        "/api/v1/attachments",
        data={"biz_type": "project", "biz_id": str(pid)},
        files={"file": ("secret.txt", "机密".encode(), "text/plain")},
        headers=_h(alice_token),
    )
    aid = up.json()["data"]["id"]

    resp = await client.get(
        f"/api/v1/attachments/{aid}/download", headers=_h(bob["token"]["access_token"])
    )
    assert resp.status_code == 404, "跨租户下载必须 404"


async def test_download_missing_attachment_returns_404(client):
    data = await _register(client, "guotao")
    resp = await client.get(
        "/api/v1/attachments/999999/download", headers=_h(data["token"]["access_token"])
    )
    assert resp.status_code == 404


# ----------------------------------------------------------------------
# 存储后端本身的路径穿越防线
# ----------------------------------------------------------------------
async def test_storage_rejects_path_traversal(tmp_path):
    """★ 数据库 path 若是穿越串，本地存储必须拒绝，而不是读到根外文件。"""
    backend = LocalStorage(root=tmp_path / "storage")

    with pytest.raises(ValueError):
        await backend.open("../../etc/passwd")

    with pytest.raises(ValueError):
        await backend.open("/etc/passwd")


async def test_storage_save_open_roundtrip(tmp_path):
    backend = LocalStorage(root=tmp_path / "storage")
    path = await backend.save(tenant_id=7, data=b"hello", ext="txt")
    assert path.startswith("7/")
    assert await backend.open(path) == b"hello"
    assert await backend.exists(path) is True

    await backend.delete(path)
    assert await backend.exists(path) is False


async def test_safe_ext():
    from app.core.storage import safe_ext

    assert safe_ext("report.pdf") == "pdf"
    assert safe_ext("archive.tar.gz") == "gz"
    assert safe_ext("noext") == ""
    assert safe_ext("UPPER.PNG") == "png"
