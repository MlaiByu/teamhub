"""Prometheus 指标测试（第 10 周步骤 1）。

★ 重点钉住两条**基数与信息泄露**相关的设计决定：

  1. **路径标签用路由模板，不是原始 path**
     `/api/v1/projects/1` 与 `/api/v1/projects/2` 必须归到同一条时间序列。
     若按原始 path 打标签，**每个资源 id 都会生成一条时间序列**——
     基数是资源总数，线上几分钟就能到百万级（Prometheus 最经典的翻车方式）。

  2. **指标标签不含 tenant_id**
     `/metrics` 通常不鉴权（Prometheus 主动来抓）。把租户维度放进去
     等于对外暴露「平台有多少租户、谁在活跃」；同时每条租户×路由×状态码
     组合都是独立时间序列，基数直接乘以租户数。

  ★ 与「缓存 key 带租户前缀」是**相反方向**的同一类判断：
    缓存要隔离（必须带），指标要脱敏（绝不能带）。
    两者都不是「随手加个字段」，而是各有明确理由的设计决定。
"""

from __future__ import annotations

import re

import pytest

from app.core import metrics as m

PASSWORD = "a-long-enough-passphrase"


@pytest.fixture(autouse=True)
def _clean_metrics():
    """指标是进程级全局状态，用例之间必须隔离。"""
    m.reset_metrics()
    yield
    m.reset_metrics()


# ======================================================================
# 渲染格式
# ======================================================================
def test_counter_renders_exposition_format():
    c = m.Counter("demo_total", "示例计数器")
    c.inc()
    c.inc(2, method="GET")

    lines = c.render()
    assert "# HELP demo_total 示例计数器" in lines
    assert "# TYPE demo_total counter" in lines
    assert "demo_total 1" in lines  # 无标签的那一次
    # inc(2, method="GET") 只累加到 method="GET" 这条序列，与无标签的不是同一条
    assert 'demo_total{method="GET"} 2' in lines


def test_histogram_renders_buckets_sum_count():
    h = m.Histogram("demo_seconds", "示例直方图", buckets=(0.1, 0.5, 1.0))
    h.observe(0.05)
    h.observe(0.3)
    h.observe(2.0)

    rendered = "\n".join(h.render())
    # 累积语义：le=0.1 只含 1 个；le=0.5 含 2 个；+Inf 含全部 3 个
    assert 'demo_seconds_bucket{le="0.1"} 1' in rendered
    assert 'demo_seconds_bucket{le="0.5"} 2' in rendered
    assert 'demo_seconds_bucket{le="1"} 2' in rendered
    assert 'demo_seconds_bucket{le="+Inf"} 3' in rendered
    assert "demo_seconds_count 3" in rendered
    assert "demo_seconds_sum 2.35" in rendered


def test_bucket_le_label_comes_last():
    """★ le 必须是标签里的最后一个（跟随 Prometheus 官方输出惯例）。

    位置不影响解析正确性（标签是无序集合），但现成的解析脚本/正则
    默认这一形态；自己发明顺序会让它们抓不到。
    """
    h = m.Histogram("pos_seconds", "x", buckets=(1.0,))
    h.observe(0.5, method="GET")
    rendered = "\n".join(h.render())
    assert 'pos_seconds_bucket{method="GET",le="1"} 1' in rendered
    assert 'pos_seconds_bucket{method="GET",le="+Inf"} 1' in rendered


def test_histogram_inf_bucket_equals_total():
    """+Inf 桶必须等于样本总数——Prometheus 靠它算分位数。"""
    h = m.Histogram("x_seconds", "x", buckets=(1.0,))
    for value in (0.1, 5.0, 9.0):
        h.observe(value)
    rendered = "\n".join(h.render())
    assert 'x_seconds_bucket{le="+Inf"} 3' in rendered
    assert "x_seconds_count 3" in rendered


def test_label_order_is_normalized():
    """★ 标签顺序不同也要归一到同一条时间序列，否则指标凭空翻倍。"""
    c = m.Counter("dup_total", "dup")
    c.inc(method="GET", path="/a")
    c.inc(path="/a", method="GET")
    assert c.value(method="GET", path="/a") == 2
    assert len(c.render()) == 3  # HELP + TYPE + 1 个样本


def test_empty_metric_still_emits_help_and_type():
    """没有样本时也要输出 HELP/TYPE，否则图表面板会因为指标不存在而报错。"""
    c = m.Counter("empty_total", "空的")
    lines = c.render()
    assert len(lines) == 2
    assert lines[0].startswith("# HELP")


def test_gauge_inc_dec():
    g = m.Gauge("inflight", "在途")
    g.inc()
    g.inc()
    g.dec()
    assert g.value() == 1


def test_render_metrics_ends_with_newline():
    """Prometheus 解析器要求最后一行以换行结束。"""
    assert m.render_metrics().endswith("\n")


# ======================================================================
# ★ 路径标签：路由模板而非原始 path
# ======================================================================
def test_route_label_falls_back_when_unmatched():
    """未匹配路由（404）时不能拿原始 URL 当标签——那会把 id 打进基数。"""
    assert m.route_label({}) == "<unmatched>"
    assert m.route_label({"route": None}) == "<unmatched>"


def test_route_label_uses_template_when_available():
    class FakeRoute:
        path = "/api/v1/projects/{project_id}"

    assert m.route_label({"route": FakeRoute()}) == "/api/v1/projects/{project_id}"


# ======================================================================
# ★ 不得泄露租户维度
# ======================================================================
async def test_metrics_endpoint_has_no_tenant_labels(client):
    """★ 指标文本里绝不能出现 tenant 字样（那等于对外公布租户清单）。"""
    data = await client.post(
        "/api/v1/auth/register", json={"username": "guotao", "password": PASSWORD}
    )
    assert data.status_code == 201
    token = data.json()["data"]["token"]["access_token"]

    await client.get("/api/v1/projects", headers={"Authorization": f"Bearer {token}"})
    resp = await client.get("/metrics")

    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "tenant_id" not in resp.text
    assert "tenant=" not in resp.text


async def test_same_route_different_ids_share_one_series(client):
    """★ 同一路由的不同资源 id 必须归到**同一条**时间序列。"""
    data = await client.post(
        "/api/v1/auth/register", json={"username": "guotao", "password": PASSWORD}
    )
    token = data.json()["data"]["token"]["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    for code in ("A-1", "A-2", "A-3"):
        await client.post("/api/v1/projects", json={"code": code, "name": code}, headers=auth)

    resp = await client.get("/metrics")
    # 明细路由的时间序列只能有一条（模板），不能是三条（三个 id）
    detail_series = [
        line
        for line in resp.text.splitlines()
        if line.startswith("http_requests_total{") and "/api/v1/projects/{project_id}" in line
    ]
    assert len(detail_series) <= 1, f"资源 id 泄漏进了标签：{detail_series}"

    # 反向：指标里不该出现任何具体的项目 code
    for code in ("A-1", "A-2", "A-3"):
        assert code not in resp.text


# ======================================================================
# 采集是否真的工作
# ======================================================================
async def test_requests_are_counted(client):
    await client.get("/health")

    resp = await client.get("/metrics")
    assert 'http_requests_total{method="GET",path="/health",status="200"}' in resp.text
    assert "http_requests_in_flight{le=" not in resp.text  # gauge 不该有 le 标签
    # 读 /metrics 的这个请求**自身正在处理中**，所以此刻在途数至少是 1。
    # 断言「等于 0」会假失败——这是个容易想当然的点。
    inflight = int(re.search(r"http_requests_in_flight (\d+)", resp.text).group(1))
    assert inflight >= 1


async def test_metrics_endpoint_itself_is_counted(client):
    await client.get("/metrics")
    resp = await client.get("/metrics")
    assert 'path="/metrics"' in resp.text


async def test_duration_histogram_records_samples(client):
    await client.get("/health")
    resp = await client.get("/metrics")
    assert "http_request_duration_seconds_count{" in resp.text
    # le 在标签末位（Prometheus 惯例）
    assert re.search(r'http_request_duration_seconds_bucket\{[^}]*le="\+Inf"\} \d+', resp.text)


async def test_unmatched_path_falls_back_to_placeholder(client):
    """404 请求也计入指标，但路径标签是占位符（不泄露被扫的 URL）。"""
    await client.get("/definitely-not-a-route/secret-token-abc")

    resp = await client.get("/metrics")
    assert 'path="<unmatched>"' in resp.text
    assert "secret-token-abc" not in resp.text
