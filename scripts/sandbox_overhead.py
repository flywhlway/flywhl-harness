#!/usr/bin/env python3
"""量系统沙箱给每条命令增加的耗时：同一条 ``true``，分别在沙箱外和各种网络策略的沙箱里跑，输出 p50、p95（毫秒）。

    python scripts/sandbox_overhead.py --runs 200 --files 2000

一次测量包括 harness 在子进程启动之外要做的准备（生成配置、起出站代理、遍历工作区找凭证位置），和实际执行命令时一样。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

from flywhl_harness.tools.confine import Confinement
from flywhl_harness.tools.sandbox import probe


async def once(box: Confinement, ws: str, env: dict[str, str]) -> float:
    start = time.perf_counter()
    async with box.enter(["/bin/sh", "-c", "true"], ws, env) as run:
        proc = await asyncio.create_subprocess_exec(*run.argv, env=run.env, cwd=ws, stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
    if proc.returncode:
        raise RuntimeError(f"命令在沙箱里失败：{proc.returncode}")
    return (time.perf_counter() - start) * 1000


def percentile(values: list[float], q: int) -> float:
    return statistics.quantiles(values, n=100, method="inclusive")[q - 1]


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", type=int, default=200)
    ap.add_argument("--files", type=int, default=2000, help="工作区里的文件数：Linux 档每条命令要遍历工作区")
    ap.add_argument("--json", help="把结果写成 JSON 文件")
    args = ap.parse_args()
    level = probe().level
    if not level:
        print(f"没有可用的系统沙箱：{probe().reason}", file=sys.stderr)
        return 1
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/")}
    rows = []
    with tempfile.TemporaryDirectory(dir=os.getcwd(), prefix=".sandbox-overhead-") as ws:
        for i in range(args.files):
            p = Path(ws, f"d{i % 100}", f"f{i}.txt")
            p.parent.mkdir(exist_ok=True)
            p.touch()
        for name, box in [("沙箱外", Confinement()), ("沙箱，网络放开", Confinement(level, "allow")),
                          ("沙箱，断网", Confinement(level, "deny")), ("沙箱，出站白名单", Confinement(level, "allowlist"))]:
            samples = [await once(box, ws, env) for _ in range(args.runs)]
            rows.append({"case": name, "p50_ms": round(percentile(samples, 50), 1), "p95_ms": round(percentile(samples, 95), 1)})
            print(f"{name:12}  p50 {rows[-1]['p50_ms']:8.1f} ms   p95 {rows[-1]['p95_ms']:8.1f} ms")
    if args.json:
        Path(args.json).write_text(json.dumps({"platform": sys.platform, "level": level, "runs": args.runs, "files": args.files,
                                               "rows": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
