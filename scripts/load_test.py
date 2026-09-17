#!/usr/bin/env python3
"""并发压测（本项目自带，不依赖 ab / wrk / hey）。

为什么自己写一个
----------------
本机没有任何系统级压测工具（`ab` / `wrk` / `hey` / `vegeta` / `locust` 全都没有），
而 `httpx` 已经是项目的测试依赖——用它写一个 **asyncio 并发**压测器，
既不用装新东西，也能把「并发数 / 时长 / 多路径 / 认证 / 读或写」都控制住。

它回答的问题
------------
1. 真实吞吐与延迟分布（QPS、p50/p90/p95/p99、最大）
2. 有没有错误（按 HTTP 状态码与异常分类统计）
3. ★ **SQLite 的写并发到底行不行**（配合 `--method POST` 打写端点）——
   这是 `docs/09` 里登记过、但一直**没有量化数字**的担忧。

用法
----
    # 读路径（默认 GET，先登录拿令牌）
    python scripts/load_test.py --base-url http://127.0.0.1:8020 \\
        --account admin --password-file .env \\
        --path /api/v1/platform/devices --path /api/v1/platform/orders \\
        --concurrency 20 --duration 20

    # 写路径（必须显式加 --method POST，避免误压生产）
    python scripts/load_test.py --base-url http://127.0.0.1:8020 \\
        --account admin --password-file .env --allow-write \\
        --method POST --path /api/v1/platform/devices/d-demo-01/simulate-heartbeat \\
        --concurrency 20 --duration 20

设计要点
--------
* **预热**：先打 `--warmup` 个请求并**丢弃其结果**，避免把连接建立与惰性初始化
  算进统计（与 `docs/10` 的 AI 基准同一套做法）。
* **写操作双重确认**：非 GET 必须显式 `--allow-write`，否则直接拒绝——
  压测误打写端点会把演示库搅乱。
* **不隐藏失败**：错误按状态码与异常类型分组打印；有任何非 2xx 时退出码为 1。
* **延迟用单调时钟**：`time.perf_counter()`，不受系统时间调整影响。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

READ_METHODS = ("GET", "HEAD")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ToyVerse Cloud 并发压测")
    parser.add_argument("--base-url", required=True, help="服务基址，如 http://127.0.0.1:8020")
    parser.add_argument("--path", action="append", required=True, help="被测路径（可重复，多路径轮询）")
    parser.add_argument("--method", default="GET", help="HTTP 方法（非 GET 需同时加 --allow-write）")
    parser.add_argument("--concurrency", type=int, default=10, help="并发虚拟用户数")
    parser.add_argument("--duration", type=float, default=10.0, help="压测持续秒数")
    parser.add_argument("--warmup", type=int, default=5, help="预热请求数（结果丢弃）")
    parser.add_argument("--timeout", type=float, default=30.0, help="单请求超时秒数")
    parser.add_argument("--account", help="登录账号；给了就会先登录并把令牌放进 Authorization 头")
    parser.add_argument("--password", help="登录口令（不推荐直接传，会进 shell 历史）")
    parser.add_argument("--password-file", help="从该文件读取登录口令（如 .env，取 *_ADMIN_PASSWORD 的第一条）")
    parser.add_argument("--body", help='JSON 请求体（如 \'{"online": true}\'）；写端点通常必填')
    parser.add_argument("--allow-write", action="store_true", help="允许对非 GET 端点压测（必须显式确认）")
    parser.add_argument("--json-out", help="把结果同时写入该 JSON 文件")
    return parser.parse_args()


def read_password(args: argparse.Namespace) -> str | None:
    """按优先级取口令：--password > --password-file。"""
    if args.password:
        return args.password
    if args.password_file:
        for line in Path(args.password_file).read_text(encoding="utf-8").splitlines():
            if line.startswith("PLATFORM_ADMIN_PASSWORD="):
                return line.split("=", 1)[1].strip()
        raise SystemExit(f"{args.password_file} 中未找到 PLATFORM_ADMIN_PASSWORD")
    return None


async def login(client: httpx.AsyncClient, base_url: str, account: str, password: str) -> str:
    resp = await client.post(
        f"{base_url}/api/v1/auth/login", json={"account": account, "password": password}
    )
    if resp.status_code != 200:
        raise SystemExit(f"登录失败：HTTP {resp.status_code} {resp.text[:200]}")
    return str(resp.json()["accessToken"])


async def one_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, object] | None = None,
) -> tuple[int | None, float, str | None]:
    """返回 (状态码 | None, 耗时秒, 异常类型名 | None)。"""
    started = time.perf_counter()
    try:
        resp = await client.request(method, url, headers=headers, json=body)
        return resp.status_code, time.perf_counter() - started, None
    except Exception as exc:  # 压测要按异常类型统计，不能因为单次失败中断整个测试
        return None, time.perf_counter() - started, type(exc).__name__


async def worker(
    client: httpx.AsyncClient,
    paths: list[str],
    method: str,
    headers: dict[str, str],
    body: dict[str, object] | None,
    deadline: float,
    results: list[tuple[int | None, float, str | None]],
    index: int,
) -> None:
    """一个虚拟用户：在截止时间前尽可能多地发请求，路径按自身序轮询。"""
    i = index
    while time.perf_counter() < deadline:
        url = paths[i % len(paths)]
        i += 1
        results.append(await one_request(client, method, url, headers, body))


def percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    k = min(len(sorted_values) - 1, max(0, int(round((pct / 100) * len(sorted_values) + 0.5)) - 1))
    return sorted_values[k]


async def main() -> int:
    args = parse_args()
    method = args.method.upper()
    if method not in READ_METHODS and not args.allow_write:
        raise SystemExit(
            f"拒绝执行：{method} 是非只读方法。若确实要压写端点，请显式加 --allow-write。"
        )
    if args.concurrency < 1:
        raise SystemExit("--concurrency 至少为 1")

    base_url = args.base_url.rstrip("/")
    paths = [p if p.startswith("/") else f"/{p}" for p in args.path]
    urls = [f"{base_url}{p}" for p in paths]

    body: dict[str, object] | None = None
    if args.body:
        try:
            parsed = json.loads(args.body)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--body 不是合法 JSON：{exc}") from exc
        if not isinstance(parsed, dict):
            raise SystemExit("--body 必须是 JSON 对象")
        body = parsed
    if body is not None and method in READ_METHODS:
        raise SystemExit(f"拒绝执行：{method} 是只读方法，却给了 --body（是否忘了 --method？）")

    limits = httpx.Limits(max_connections=args.concurrency * 2, max_keepalive_connections=args.concurrency)
    async with httpx.AsyncClient(timeout=args.timeout, limits=limits) as client:
        headers: dict[str, str] = {}
        if args.account:
            password = read_password(args)
            if not password:
                raise SystemExit("给了 --account 就必须同时给 --password 或 --password-file")
            headers["Authorization"] = f"Bearer {await login(client, base_url, args.account, password)}"
            print(f"已登录：{args.account}")

        print(f"\n目标 {base_url}  方法 {method}  路径 {paths}")
        print(f"并发 {args.concurrency}  时长 {args.duration}s  预热 {args.warmup}  超时 {args.timeout}s")

        # ---- 预热（结果丢弃）----
        for i in range(args.warmup):
            await one_request(client, method, urls[i % len(urls)], headers, body)

        # ---- 正式压测 ----
        results: list[tuple[int | None, float, str | None]] = []
        deadline = time.perf_counter() + args.duration
        started_at = time.perf_counter()
        await asyncio.gather(
            *(
                worker(client, urls, method, headers, body, deadline, results, i)
                for i in range(args.concurrency)
            )
        )
        elapsed = time.perf_counter() - started_at

    # ---- 统计 ----
    total = len(results)
    latencies = sorted(r[1] for r in results)
    ok = sum(1 for r in results if r[0] is not None and 200 <= r[0] < 300)
    status_counts = Counter(str(r[0]) if r[0] is not None else f"异常:{r[2]}" for r in results)
    failed = total - ok

    report = {
        "baseUrl": base_url,
        "method": method,
        "paths": paths,
        "concurrency": args.concurrency,
        "durationSec": round(elapsed, 2),
        "totalRequests": total,
        "okRequests": ok,
        "failedRequests": failed,
        "qps": round(total / elapsed, 1) if elapsed else 0.0,
        "latencyMs": {
            "p50": round(percentile(latencies, 50) * 1000, 2),
            "p90": round(percentile(latencies, 90) * 1000, 2),
            "p95": round(percentile(latencies, 95) * 1000, 2),
            "p99": round(percentile(latencies, 99) * 1000, 2),
            "max": round((latencies[-1] if latencies else 0) * 1000, 2),
            "mean": round(statistics.fmean(latencies) * 1000, 2) if latencies else 0.0,
        },
        "statusCodes": dict(status_counts.most_common()),
    }

    print("\n================ 结果 ================")
    print(f"总请求 {total}  成功 {ok}  失败 {failed}  实际耗时 {elapsed:.2f}s")
    print(f"吞吐 QPS = {report['qps']}")
    lat = report["latencyMs"]
    print(f"延迟(ms)  p50={lat['p50']}  p90={lat['p90']}  p95={lat['p95']}  p99={lat['p99']}  max={lat['max']}  mean={lat['mean']}")
    print(f"状态码分布 {report['statusCodes']}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已写入 {args.json_out}")

    if failed:
        print(f"\n✘ 有 {failed} 个请求未成功——见上面的状态码分布")
        return 1
    print("\n✔ 全部请求成功")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
