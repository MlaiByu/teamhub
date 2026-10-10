"""Prometheus 指标（零依赖实现，PROJECT-PLAN 11.4）。

★ 为什么手写 exposition 格式，而不是引入 `prometheus_client`：

  项目的硬要求之一是「零依赖降级」——clone 下来不配任何外部服务就能跑。
  `prometheus_client` 虽然也是纯 Python，但它是一个**只为暴露指标而存在**的
  依赖；而 exposition 格式本身极简单（几行文本：`# HELP` / `# TYPE` /
  `metric{label="v"} value`）。手写的成本远低于新增一个依赖，
  而且顺带说明白了「指标到底是什么」。

★★ 两个关键设计决定（都写在这里，因为它们容易被做错）：

  1. **指标标签不带 `tenant_id`**。任一条理由都足够：
     · **基数爆炸**：租户 × 路由 × 状态码 = 每条组合一条独立时间序列。
       租户一多，Prometheus 的存储与查询就会被拉爆——
       这是 Prometheus 最常见的翻车方式（"cardinality explosion"）。
     · **信息泄露**：`/metrics` 通常**不鉴权**（由 Prometheus 主动来抓），
       把租户维度放进去等于对外暴露「平台有多少租户、谁在活跃」。

     真需要按租户看用量，应该走**业务侧统计接口**（查库、要鉴权），
     而不是把它塞进监控指标。

  2. **路径标签用「路由模板」而不是原始 path**：`/api/v1/projects/123` 与
     `/api/v1/projects/456` 属于同一个路由，若按原始 path 打标签，
     **每个资源 id 都会生成一条时间序列**——基数等于资源总数，
     几分钟就能到百万级。所以取 `request.scope["route"].path`（模板），
     取不到（如 404 未匹配）时退化成 `<unmatched>`。

★ 线程/协程安全性：本模块只在 asyncio 单线程里被写。
  计数与字典更新之间**没有 await**，所以不会被打断、无需加锁。
  （若要在线程池里写，必须补锁。）
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

# Prometheus 默认的直方图分桶（秒）。覆盖「本地内存库的微秒级」
# 到「生产慢查询的秒级」，便于直接对比压测结果。
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

LabelKey = tuple[tuple[str, str], ...]


def _sorted_labels(labels: dict[str, str]) -> LabelKey:
    """标签按名字排序。

    ★ 必须排序：`{method="GET",path="/x"}` 与 `{path="/x",method="GET"}`
      是**同一个**时间序列，若不归一化会被当成两条——指标凭空翻倍。
    """
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _render_labels(key: LabelKey) -> str:
    if not key:
        return ""
    inner = ",".join(f'{name}="{value}"' for name, value in key)
    return "{" + inner + "}"


def _render_bucket_labels(key: LabelKey, upper: str) -> str:
    """直方图分桶的标签渲染。

    ★ `le` 强制排在**最后**：标签在 Prometheus 里是无序集合（位置不影响
      正确性），但官方输出的惯例是 `le` 收尾，许多现成的解析脚本/正则
      也默认这一形态。写成 `{le="1",method="GET"}` 虽然合法，
      却会让那些脚本抓不到——所以跟随惯例，而不是自己发明顺序。
    """
    parts = [f'{name}="{value}"' for name, value in key]
    parts.append(f'le="{upper}"')
    return "{" + ",".join(parts) + "}"


def _escape_help(text: str) -> str:
    # exposition 的 HELP 行里反斜杠与换行需要转义
    return text.replace("\\", "\\\\").replace("\n", "\\n")


class Counter:
    """单调递增计数器。"""

    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help_text = help_text
        self._values: dict[LabelKey, float] = defaultdict(float)

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        self._values[_sorted_labels(labels)] += amount

    def value(self, **labels: str) -> float:
        return self._values.get(_sorted_labels(labels), 0.0)

    def render(self) -> list[str]:
        lines = [
            f"# HELP {self.name} {_escape_help(self.help_text)}",
            f"# TYPE {self.name} counter",
        ]
        if not self._values:
            # 一个样本都没有时也要输出 HELP/TYPE：图表面板才不会因为
            # 指标「不存在」而报错（Prometheus 里不存在的指标是查不到的）。
            return lines
        for key, value in sorted(self._values.items()):
            lines.append(f"{self.name}{_render_labels(key)} {value:g}")
        return lines

    def reset(self) -> None:
        self._values.clear()


class Gauge:
    """可增可减的瞬时值。"""

    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help_text = help_text
        self._values: dict[LabelKey, float] = defaultdict(float)

    def set(self, value: float, **labels: str) -> None:
        self._values[_sorted_labels(labels)] = value

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        self._values[_sorted_labels(labels)] += amount

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        self._values[_sorted_labels(labels)] -= amount

    def value(self, **labels: str) -> float:
        return self._values.get(_sorted_labels(labels), 0.0)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {_escape_help(self.help_text)}", f"# TYPE {self.name} gauge"]
        if not self._values:
            return lines
        for key, value in sorted(self._values.items()):
            lines.append(f"{self.name}{_render_labels(key)} {value:g}")
        return lines

    def reset(self) -> None:
        self._values.clear()


class Histogram:
    """累积直方图（Prometheus 的经典形式：bucket / sum / count）。"""

    def __init__(
        self,
        name: str,
        help_text: str,
        buckets: tuple[float, ...] = DEFAULT_BUCKETS,
    ) -> None:
        self.name = name
        self.help_text = help_text
        self.buckets = tuple(sorted(buckets))
        self._counts: dict[LabelKey, list[int]] = {}
        self._sums: dict[LabelKey, float] = defaultdict(float)
        self._totals: dict[LabelKey, int] = defaultdict(int)

    def observe(self, value: float, **labels: str) -> None:
        key = _sorted_labels(labels)
        if key not in self._counts:
            self._counts[key] = [0] * len(self.buckets)
        # 累积分桶：每个桶记录「小于等于该上界」的样本数
        for index, upper in enumerate(self.buckets):
            if value <= upper:
                self._counts[key][index] += 1
        self._sums[key] += value
        self._totals[key] += 1

    def count(self, **labels: str) -> int:
        return self._totals.get(_sorted_labels(labels), 0)

    def render(self) -> list[str]:
        lines = [
            f"# HELP {self.name} {_escape_help(self.help_text)}",
            f"# TYPE {self.name} histogram",
        ]
        if not self._counts:
            return lines
        for key in sorted(self._counts):
            for index, upper in enumerate(self.buckets):
                lines.append(
                    f"{self.name}_bucket{_render_bucket_labels(key, f'{upper:g}')} "
                    f"{self._counts[key][index]}"
                )
            # +Inf 桶等于样本总数（Prometheus 靠它算分位数）
            lines.append(
                f"{self.name}_bucket{_render_bucket_labels(key, '+Inf')} {self._totals[key]}"
            )
            lines.append(f"{self.name}_sum{_render_labels(key)} {self._sums[key]:g}")
            lines.append(f"{self.name}_count{_render_labels(key)} {self._totals[key]}")
        return lines

    def reset(self) -> None:
        self._counts.clear()
        self._sums.clear()
        self._totals.clear()


# ======================================================================
# 本项目的指标（模块级单例）
# ======================================================================
HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "HTTP 请求总数（含被限流拒绝的请求）。",
)

HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP 请求处理耗时（秒）。",
)

HTTP_REQUESTS_IN_FLIGHT = Gauge(
    "http_requests_in_flight",
    "当前正在处理中的 HTTP 请求数。",
)

_ALL: tuple[Any, ...] = (HTTP_REQUESTS_TOTAL, HTTP_REQUEST_DURATION, HTTP_REQUESTS_IN_FLIGHT)


def render_metrics() -> str:
    """渲染成 Prometheus exposition 文本（text/plain; version=0.0.4）。"""
    lines: list[str] = []
    for metric in _ALL:
        lines.extend(metric.render())
    # 尾部换行：Prometheus 的解析器要求最后一行以换行结束
    return "\n".join(lines) + "\n"


def reset_metrics() -> None:
    """清空所有指标（测试用）。

    ★ 测试必须能重置：指标是**进程级全局状态**，若不重置，
      上一个用例的计数会带到下一个（「单独跑绿、一起跑红」的经典成因）。
    """
    for metric in _ALL:
        metric.reset()


def route_label(scope: Mapping[str, Any]) -> str:
    """从 ASGI scope 里取「路由模板」作为 path 标签。

    取不到时（404 未匹配任何路由、中间件短路等）返回 `<unmatched>`，
    **绝不用原始 URL**——原始 URL 里含资源 id，会把时间序列基数打爆。
    """
    route = scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    return "<unmatched>"
