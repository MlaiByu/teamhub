"""零依赖压测脚本（第 10 周）。

★ 为什么不用 locust：

  方案把 locust 标为「可选，非必须」，而本项目有一条更硬的约束——
  **零依赖降级**。引入 locust 会带来一长串传递依赖（gevent、flask、
  msgpack…），只为了跑一次压测并不划算。

  压测真正需要的东西其实很少：并发发请求 + 记录延迟。httpx（项目已有，
  测试依赖）+ asyncio 就能做到，而且**结果口径完全由自己掌握**
  （分位数怎么算、错误怎么归类都清楚）。

★ 口径说明（读报告前先看这里，避免误读数字）：

  · **延迟包含客户端开销**（序列化、TCP、事件循环调度），
    所以它比服务端 `http_request_duration_seconds` 略高——
    压测数字是「端到端体感」，不是纯服务端处理时间。
  · **并发靠 asyncio 协程**，不是多线程/多进程。
    因此它测的是「单进程 async 服务的 I/O 并发能力」；
    真要用满多核需要多进程压测端，本脚本不做（单机压测端本身会成为瓶颈）。
  · **分位数用最近秩法**（nearest-rank）而不是插值，样本少时更稳。

用法：

    python -m scripts.loadtest                       # 默认场景
    python -m scripts.loadtest --concurrency 50 --total 2000
    python -m scripts.loadtest --json out.json       # 同时落一份机器可读结果
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
# noqa: S105 —— 这是压测用的临时账号口令，不是任何真实凭据
PASSWORD = "a-long-enough-passphrase"  # noqa: S105


# ======================================================================
# 结果统计
# ======================================================================
@dataclass
class BenchResult:
    name: str
    concurrency: int
    total: int
    wall_seconds: float
    latencies_ms: list[float] = field(repr=False)
    status_counts: dict[str, int] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)

    # ---- 派生指标 ----
    @property
    def succeeded(self) -> int:
        return sum(count for status, count in self.status_counts.items() if status.startswith("2"))

    @property
    def failed(self) -> int:
        return self.total - self.succeeded

    @property
    def qps(self) -> float:
        return self.total / self.wall_seconds if self.wall_seconds else 0.0

    @property
    def error_rate(self) -> float:
        return self.failed / self.total if self.total else 0.0

    def percentile(self, pct: float) -> float:
        """最近秩法分位数（样本少时比插值稳）。"""
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        index = max(0, min(len(ordered) - 1, round(pct / 100 * len(ordered) + 0.5) - 1))
        return ordered[index]

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "concurrency": self.concurrency,
            "total": self.total,
            "wall_seconds": round(self.wall_seconds, 3),
            "qps": round(self.qps, 1),
            "succeeded": self.succeeded,
            "failed": self.failed,
            "error_rate": round(self.error_rate, 4),
            "latency_ms": {
                "min": round(min(self.latencies_ms), 2) if self.latencies_ms else 0,
                "mean": round(statistics.fmean(self.latencies_ms), 2) if self.latencies_ms else 0,
                "p50": round(self.percentile(50), 2),
                "p95": round(self.percentile(95), 2),
                "p99": round(self.percentile(99), 2),
                "max": round(max(self.latencies_ms), 2) if self.latencies_ms else 0,
            },
            "status_counts": self.status_counts,
            "errors": self.errors,
        }


# ======================================================================
# 压测执行
# ======================================================================
async def run_bench(
    *,
    name: str,
    client: httpx.AsyncClient,
    request_fn: Callable[[httpx.AsyncClient], Awaitable[httpx.Response]],
    concurrency: int,
    total: int,
) -> BenchResult:
    """固定总请求数、受信号量限制并发地打请求。"""
    latencies: list[float] = []
    status_counts: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    semaphore = asyncio.Semaphore(concurrency)

    async def one() -> None:
        async with semaphore:
            started = time.perf_counter()
            try:
                response = await request_fn(client)
                status_counts[str(response.status_code)] += 1
            except Exception as exc:  # 压测里任何异常都算一次失败样本
                errors[type(exc).__name__] += 1
                status_counts["<exception>"] += 1
            finally:
                latencies.append((time.perf_counter() - started) * 1000)

    wall_started = time.perf_counter()
    await asyncio.gather(*(one() for _ in range(total)))
    wall = time.perf_counter() - wall_started

    return BenchResult(
        name=name,
        concurrency=concurrency,
        total=total,
        wall_seconds=wall,
        latencies_ms=latencies,
        status_counts=dict(status_counts),
        errors=dict(errors),
    )


def print_result(result: BenchResult) -> None:
    s = result.summary()
    latency = s["latency_ms"]
    print(f"\n{'=' * 66}")
    print(f"场景: {s['name']}")
    print(f"{'=' * 66}")
    print(f"  并发        : {s['concurrency']}")
    print(f"  请求总数    : {s['total']}（成功 {s['succeeded']} / 失败 {s['failed']}）")
    print(f"  总耗时      : {s['wall_seconds']}s")
    print(f"  QPS         : {s['qps']}")
    print(f"  错误率      : {s['error_rate'] * 100:.2f}%")
    print(
        f"  延迟(ms)    : min={latency['min']} mean={latency['mean']} "
        f"p50={latency['p50']} p95={latency['p95']} p99={latency['p99']} max={latency['max']}"
    )
    print(f"  状态码分布  : {s['status_counts']}")
    if s["errors"]:
        print(f"  异常类型    : {s['errors']}")


# ======================================================================
# 场景定义（真实业务链路，不是只打 /health）
# ======================================================================
async def prepare_tenant(client: httpx.AsyncClient) -> dict[str, Any]:
    """建一个**全新的**租户 + 一些数据，供读场景使用。

    ★ 用户名必须唯一（加随机后缀）：压测库通常是复用的，
      固定用户名第二次跑就会撞 `409 Conflict`，
      而那个报错看起来像「服务有问题」，实际只是脚本没有幂等性。
    """
    suffix = uuid4().hex[:8]
    register = await client.post(
        "/api/v1/auth/register",
        json={"username": f"bench-{suffix}", "password": PASSWORD},
    )
    register.raise_for_status()
    data = register.json()["data"]
    token = data["token"]["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 预置 20 个项目，让「列表查询」有真实的数据量（不是空表返回）
    for index in range(20):
        await client.post(
            "/api/v1/projects",
            json={"code": f"BENCH-{suffix}-{index:03d}", "name": f"压测项目 {index}"},
            headers=headers,
        )
    return {"token": token, "headers": headers, "tenant_id": data["tenant"]["id"]}


async def main() -> int:
    parser = argparse.ArgumentParser(description="零依赖压测（httpx + asyncio）")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--total", type=int, default=500)
    parser.add_argument("--json", dest="json_path", default=None)
    parser.add_argument(
        "--scenario",
        choices=["all", "health", "list", "mixed"],
        default="all",
        help="health=最轻路径；list=带鉴权的列表查询（走完整中间件+DB+缓存）；mixed=混合",
    )
    args = parser.parse_args()

    timeout = httpx.Timeout(30.0)
    limits = httpx.Limits(
        max_connections=args.concurrency * 2, max_keepalive_connections=args.concurrency
    )
    results: list[BenchResult] = []

    # ★ 压测本机服务时**必须忽略环境里的代理**：
    #   开发机上常见的 `http_proxy` / `all_proxy` 会把 `127.0.0.1` 也送进代理，
    #   结果是 502 Bad Gateway（而不是连接被拒）——一个非常像「服务没起来」的
    #   假象，会让人往错误的方向排查。
    host = httpx.URL(args.base_url).host or ""
    # noqa: S104 —— 这里只是把 "0.0.0.0" 当字符串比对，并没有绑定端口
    is_local = host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")  # noqa: S104
    if is_local:
        print("[提示] 目标是本机地址，已忽略环境代理（trust_env=False）")

    async with httpx.AsyncClient(
        base_url=args.base_url,
        timeout=timeout,
        limits=limits,
        trust_env=not is_local,
    ) as client:
        # 先探活，避免把「服务没起来」当成压测结果
        try:
            probe = await client.get("/health")
            probe.raise_for_status()
        except Exception as exc:
            print(f"[!] 无法连接 {args.base_url}：{exc}", file=sys.stderr)
            print("    请先启动服务（uvicorn app.main:app）再跑压测。", file=sys.stderr)
            return 2

        context = await prepare_tenant(client)
        headers = context["headers"]
        print(f"[准备完成] 租户 {context['tenant_id']}，预置 20 个项目")

        if args.scenario in ("all", "health"):
            results.append(
                await run_bench(
                    name="GET /health（最轻路径，不含 DB / 鉴权）",
                    client=client,
                    request_fn=lambda c: c.get("/health"),
                    concurrency=args.concurrency,
                    total=args.total,
                )
            )

        if args.scenario in ("all", "list"):
            results.append(
                await run_bench(
                    name="GET /api/v1/projects（鉴权 + DB + 限流 + 指标采集）",
                    client=client,
                    request_fn=lambda c: c.get("/api/v1/projects", headers=headers),
                    concurrency=args.concurrency,
                    total=args.total,
                )
            )

        if args.scenario in ("all", "mixed"):
            counter = {"n": 0}

            async def mixed(c: httpx.AsyncClient) -> httpx.Response:
                counter["n"] += 1
                if counter["n"] % 5 == 0:  # 20% 写、80% 读
                    return await c.post(
                        "/api/v1/projects",
                        json={"code": f"MIX-{counter['n']}", "name": "混合场景"},
                        headers=headers,
                    )
                return await c.get("/api/v1/projects", headers=headers)

            results.append(
                await run_bench(
                    name="混合（80% 列表读 + 20% 建项目写）",
                    client=client,
                    request_fn=mixed,
                    concurrency=args.concurrency,
                    total=args.total,
                )
            )

    for result in results:
        print_result(result)

    if args.json_path:
        payload = {
            "base_url": args.base_url,
            "concurrency": args.concurrency,
            "total_per_scenario": args.total,
            "results": [r.summary() for r in results],
        }
        # noqa: ASYNC230 —— 结果文件在压测结束后一次性写出，
        # 阻塞几毫秒不影响任何测量结果（此时所有请求都已结束）
        with open(args.json_path, "w", encoding="utf-8") as handle:  # noqa: ASYNC230
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        print(f"\n[已写出] {args.json_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
