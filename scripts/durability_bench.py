#!/usr/bin/env python3
"""量会话库两种落盘强度的代价：跑真实的 Agent 循环，比较 durability = "full" 和 "normal"。

    uv run python scripts/durability_bench.py --rounds 3 --json out.json

模型是不花钱、不要密钥的 ScriptedProvider，其余都是真的：流式事件经 EventHub 落库，工具 intent 和结果写 steps，
bash 真的起子进程，写操作前打影子 git 快照。工具只在临时工作区里跑 echo 和 touch。

两种节奏（--profiles）：
- burst：模型零延迟，提交一个接一个，是这个库能遇到的最坏情况；
- paced：每个流式分片隔 10ms，接近快模型的输出速度，事件走 50ms 一批的落库路径。

指标：每轮耗时（每个会话平均跑完一个模型回合要多久）、每秒事务数、提交耗时分位数、事件循环延迟（周期性任务的实际唤醒偏差）。
轮次之间交替两种设置的先后，抵消温度和后台负载的漂移。

脚本先新建一个临时目录，把 HOME、TMPDIR、FLYWHL_HOME、工作目录、工作区都指进去，结束时只删这一个目录。
临时目录所在的盘就是被测的盘，--parent 可以换位置；tmpfs 上 fsync 不起作用，脚本拒绝运行。
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import platform
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

PREFIX = "flywhl-durability-bench-"
LAG_INTERVAL = 0.005
PACED_DELAY = 0.01
THINKING = "先看一下工作区现在的状态，确认上一步的结果，再决定这一步要执行什么命令。" * 2
TEXT = "上一步已经完成，结果符合预期。接下来继续执行计划里的下一项，完成后再核对一次输出。" * 3
WRITES = ("INSERT", "UPDATE", "DELETE", "REPLAC")
# 判定标准：相对 normal，full 的事件循环延迟 p99 增加量、循环最大卡顿、每轮吞吐下降比例
LIMIT_P99_MS, LIMIT_MAX_MS, LIMIT_DROP = 50, 500, 0.30


def isolate(parent: str | None) -> Path:
    """新建临时目录并把环境全部指进去。用户配置、信任记录、外面的 FLYWHL_* 设置都不会影响结果。"""
    base = Path(tempfile.mkdtemp(prefix=PREFIX, dir=parent)).resolve()
    for name in ("home", "tmp", "flywhl-home", "cwd", "runs"):
        (base / name).mkdir()
    for key in list(os.environ):
        if key.startswith(("FLYWHL_", "LLM_")):
            del os.environ[key]
    os.environ.update(HOME=str(base / "home"), TMPDIR=str(base / "tmp"), FLYWHL_HOME=str(base / "flywhl-home"))
    tempfile.tempdir = None
    os.chdir(base / "cwd")
    return base


def clean_up(base: Path) -> None:
    """只删 isolate 建的那个目录：名字对得上才动手，软链接不跟。"""
    os.chdir("/")
    if base.name.startswith(PREFIX) and base.is_dir() and not base.is_symlink():
        shutil.rmtree(base)


def filesystem(where: Path) -> str:
    """where 所在的文件系统类型。macOS 的 mount 输出是 ``on / (apfs, ...)``，Linux 是 ``on / type ext4 (...)``。"""
    try:
        mountpoint = subprocess.run(["df", "-P", str(where)], capture_output=True, text=True, check=True).stdout.split()[-1]
        out = subprocess.run(["mount"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    found = re.findall(rf" on {re.escape(mountpoint)} (?:type (\S+)|\((\w+))", out)
    return (found[-1][0] or found[-1][1]) if found else "unknown"


def percentile(values: list[float], q: int) -> float:
    return statistics.quantiles(values, n=100, method="inclusive")[q - 1] if len(values) > 1 else values[0]


def calibrate(where: Path, n: int = 200) -> dict[str, float]:
    """同一块盘上直接量一次同步要多久。full 的提交耗时应该落在 fullfsync 这个量级，低得多就说明设置没有真的起作用。"""
    syncs: dict[str, Any] = {"fsync": os.fsync}
    if hasattr(fcntl, "F_FULLFSYNC"):
        syncs["fullfsync"] = lambda fd: fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    out: dict[str, float] = {}
    with open(where / "calibrate.bin", "wb", buffering=0) as f:
        for name, sync in syncs.items():
            samples = []
            for _ in range(n):
                f.write(b"\0" * 4096)
                start = time.perf_counter()
                sync(f.fileno())
                samples.append((time.perf_counter() - start) * 1000)
            out[f"{name}_p50_ms"] = round(percentile(samples, 50), 3)
            out[f"{name}_p99_ms"] = round(percentile(samples, 99), 3)
    return out


class TimedConn:
    """包住 Store 的连接，记下每次提交的耗时。Store 在事务外的写语句是自动提交，同样算一次提交。"""

    def __init__(self, conn: sqlite3.Connection, store: Any, sink: list[float]):
        self._conn, self._store, self._sink = conn, store, sink

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def execute(self, sql: str, *params: Any) -> sqlite3.Cursor:
        head = sql.lstrip()[:6].upper()
        if head != "COMMIT" and not (self._store._depth == 0 and head in WRITES):
            return self._conn.execute(sql, *params)
        start = time.perf_counter()
        cur = self._conn.execute(sql, *params)
        self._sink.append(time.perf_counter() - start)
        return cur


async def sample_lag(samples: list[float]) -> None:
    """事件循环被卡住时这个任务醒不过来，实际唤醒时间减去计划时间就是延迟。"""
    loop = asyncio.get_running_loop()
    while True:
        start = loop.time()
        await asyncio.sleep(LAG_INTERVAL)
        samples.append(loop.time() - start - LAG_INTERVAL)


def script(turns: int) -> list[Any]:
    """每个回合一段思考、一段文本、两个工具调用。命令逐轮不同，免得触发重复调用检测。"""
    calls = [{"thinking": THINKING, "text": TEXT, "tool_calls": [
        {"name": "bash", "args": {"command": f"echo turn-{i}"}},
        {"name": "bash", "args": {"command": f"touch marker-{i}.txt"}},
    ]} for i in range(turns)]
    return [*calls, "全部完成。"]


def expect_pragmas(store: Any, durability: str) -> dict[str, int]:
    """读回连接上实际生效的值，对不上就别往下量了：那样比较的就不是这两种设置。"""
    got = {p: store._q(f"PRAGMA {p}")[0][0] for p in ("synchronous", "fullfsync", "checkpoint_fullfsync")}
    want = {"synchronous": 2, "fullfsync": 1, "checkpoint_fullfsync": 1} if durability == "full" else {"synchronous": 1, "fullfsync": 0}
    if any(got[k] != v for k, v in want.items()):
        raise RuntimeError(f"{durability} 的 PRAGMA 没有生效：{got}")
    return got


async def measure(profile: str, durability: str, sessions: int, turns: int, sandbox: str, base: Path) -> dict[str, Any]:
    from flywhl_harness import Harness, HarnessConfig, RunStatus, ScriptedProvider

    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=base / "runs"))
    root = run_dir / "workspace"
    workspaces = [root / f"s{i}" for i in range(sessions)]
    for ws in workspaces:
        ws.mkdir(parents=True)
    cfg = HarnessConfig.load(path=run_dir / "no-such-config.toml", workspace=str(root), durability=durability)
    cfg.permissions.on_ask = "allow"    # 无人值守：审批记录照常落库，不等人
    cfg.permissions.sandbox = sandbox
    cfg.load_agents_md = cfg.load_skills = False
    cfg.budget.max_turns = turns + 5
    cfg.budget.max_tool_calls = turns * 3
    provider = ScriptedProvider(script(turns), chunk=8, delay=PACED_DELAY if profile == "paced" else 0.0)
    h = Harness(cfg, provider=provider, model="scripted")
    applied = expect_pragmas(h.store, durability)
    commits: list[float] = []
    h.store._conn = TimedConn(h.store._conn, h.store, commits)
    lags: list[float] = []
    sampler = asyncio.create_task(sample_lag(lags))
    async with h:
        made = [h.create_session(f"压测 {i}", workspace=str(ws)) for i, ws in enumerate(workspaces)]
        start = time.perf_counter()
        runs = [await h.start_run(s.id, "按计划跑完") for s in made]
        done = await asyncio.wait_for(asyncio.gather(*(h.wait(r.id) for r in runs)), timeout=max(300, turns * sessions * 5))
        wall = time.perf_counter() - start
    sampler.cancel()
    # 工具没有真的跑、Run 没有正常结束的话，这一轮更快也没有意义
    for rec, ws in zip(done, workspaces):
        steps = h.store.list_steps(rec.id)
        if rec.status != RunStatus.COMPLETED or len(steps) != turns * 2 or any(s.status != "ok" for s in steps):
            raise RuntimeError(f"{rec.id} 状态 {rec.status}（{rec.reason}），{len(steps)} 个工具步骤：{[s.status for s in steps][:4]}")
        if len(list(ws.glob("marker-*.txt"))) != turns:
            raise RuntimeError(f"{ws} 里的标记文件数量不对，touch 没有全部执行")
    events = h.store._q("SELECT COUNT(*) FROM events")[0][0]
    db = Path(h.store.db_path)
    db_bytes, wal_bytes = db.stat().st_size, db.with_name(db.name + "-wal").stat().st_size
    h.store.close()
    commit_ms = [c * 1000 for c in commits]
    lag_ms = [max(x, 0.0) * 1000 for x in lags]
    return {
        "profile": profile, "durability": durability, "sessions": sessions, "turns": turns, "pragmas": applied,
        "load_1m": round(os.getloadavg()[0], 1),    # 机器上别的负载也在抢盘，这个数字用来判断结果有多可信
        "wall_s": round(wall, 3), "turn_ms": round(wall / turns * 1000, 2), "turns_per_s": round(sessions * turns / wall, 2),
        "commits": len(commit_ms), "tx_per_s": round(len(commit_ms) / wall, 1), "events": events,
        "commit_total_s": round(sum(commit_ms) / 1000, 3),
        "commit_p50_ms": round(percentile(commit_ms, 50), 3), "commit_p95_ms": round(percentile(commit_ms, 95), 3),
        "commit_p99_ms": round(percentile(commit_ms, 99), 3), "commit_max_ms": round(max(commit_ms), 3),
        "lag_samples": len(lag_ms), "lag_p50_ms": round(percentile(lag_ms, 50), 3), "lag_p99_ms": round(percentile(lag_ms, 99), 3),
        "lag_max_ms": round(max(lag_ms), 3),
        # 主库文件比一个空库大，说明跑过检查点：WAL 写满 1000 页才触发，检查点的同步才算量到了
        "db_bytes": db_bytes, "wal_bytes": wal_bytes,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[tuple[str, int, str], dict[str, float]]:
    """同一种情况各轮取中位数。最大卡顿例外：取所有轮次里最坏的一次。"""
    groups: dict[tuple[str, int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row["profile"], row["sessions"], row["durability"]].append(row)
    out = {}
    for key, group in groups.items():
        med = {m: statistics.median(r[m] for r in group) for m in
               ("turn_ms", "turns_per_s", "tx_per_s", "commit_p50_ms", "commit_p99_ms", "lag_p50_ms", "lag_p99_ms")}
        out[key] = {**med, "commit_max_ms": max(r["commit_max_ms"] for r in group), "lag_max_ms": max(r["lag_max_ms"] for r in group),
                    "rounds": len(group)}
    return out


def verdicts(summary: dict[tuple[str, int, str], dict[str, float]]) -> list[dict[str, Any]]:
    out = []
    for (profile, sessions) in sorted({k[:2] for k in summary}):
        full, normal = summary.get((profile, sessions, "full")), summary.get((profile, sessions, "normal"))
        if not full or not normal:
            continue
        p99_delta = full["lag_p99_ms"] - normal["lag_p99_ms"]
        drop = 1 - full["turns_per_s"] / normal["turns_per_s"]
        out.append({"profile": profile, "sessions": sessions, "lag_p99_delta_ms": round(p99_delta, 2),
                    "lag_max_full_ms": full["lag_max_ms"], "throughput_drop": round(drop, 4),
                    "passed": p99_delta <= LIMIT_P99_MS and full["lag_max_ms"] <= LIMIT_MAX_MS and drop <= LIMIT_DROP})
    return out


def report(summary: dict[tuple[str, int, str], dict[str, float]], judged: list[dict[str, Any]]) -> None:
    print("\n| 节奏 | 会话 | 设置 | 轮数 | 每轮耗时 ms | 回合/秒 | 事务/秒 | 提交 p50 ms | 提交 p99 ms | 提交最大 ms | 循环 p50 ms | 循环 p99 ms | 循环最大 ms |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for (profile, sessions, durability), s in sorted(summary.items()):
        print(f"| {profile} | {sessions} | {durability} | {s['rounds']} | {s['turn_ms']:.1f} | {s['turns_per_s']:.2f} | {s['tx_per_s']:.0f} "
              f"| {s['commit_p50_ms']:.2f} | {s['commit_p99_ms']:.2f} | {s['commit_max_ms']:.1f} "
              f"| {s['lag_p50_ms']:.2f} | {s['lag_p99_ms']:.2f} | {s['lag_max_ms']:.1f} |")
    print(f"\n| 节奏 | 会话 | 循环 p99 增加 ms（≤{LIMIT_P99_MS}） | full 循环最大 ms（≤{LIMIT_MAX_MS}） | 吞吐下降（≤{LIMIT_DROP:.0%}） | 判定 |")
    print("|---|---|---|---|---|---|")
    for v in judged:
        print(f"| {v['profile']} | {v['sessions']} | {v['lag_p99_delta_ms']:.2f} | {v['lag_max_full_ms']:.1f} "
              f"| {v['throughput_drop']:.1%} | {'通过' if v['passed'] else '超标'} |")


async def run_all(args: argparse.Namespace, base: Path) -> list[dict[str, Any]]:
    rows = []
    total = args.rounds * len(args.profiles) * len(args.sessions) * len(args.durability)
    for r in range(args.rounds):
        order = args.durability if r % 2 == 0 else args.durability[::-1]
        for profile in args.profiles:
            for sessions in args.sessions:
                for durability in order:
                    row = {"round": r + 1, **await measure(profile, durability, sessions, args.turns, args.sandbox, base)}
                    rows.append(row)
                    print(f"[{len(rows)}/{total}] 第 {r + 1} 轮 {profile} {sessions} 会话 {durability}：{row['wall_s']}s，"
                          f"提交 p50 {row['commit_p50_ms']}ms p99 {row['commit_p99_ms']}ms，"
                          f"循环 p99 {row['lag_p99_ms']}ms 最大 {row['lag_max_ms']}ms", flush=True)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--turns", type=int, default=60, help="每个会话的模型回合数。要够大，WAL 写满 1000 页才会触发检查点")
    ap.add_argument("--sessions", type=int, nargs="+", default=[1, 4])
    ap.add_argument("--profiles", nargs="+", choices=("burst", "paced"), default=["burst", "paced"])
    ap.add_argument("--durability", nargs="+", choices=("full", "normal"), default=["full", "normal"])
    ap.add_argument("--sandbox", choices=("auto", "off"), default="auto", help="容器里没有可用的 bubblewrap 时用 off")
    ap.add_argument("--parent", help="临时目录建在哪里，默认系统临时目录。它所在的盘就是被测的盘")
    ap.add_argument("--json", help="把结果写成 JSON 文件")
    args = ap.parse_args()
    out = Path(args.json).resolve() if args.json else None
    base = isolate(args.parent)
    try:
        fs = filesystem(base)
        if fs in ("tmpfs", "ramfs"):
            print(f"{base} 在 {fs} 上，fsync 不起作用，量出来没有意义", file=sys.stderr)
            return 1
        env = {"platform": platform.platform(), "machine": platform.machine(), "python": platform.python_version(),
               "sqlite": sqlite3.sqlite_version, "filesystem": fs, "sandbox": args.sandbox, "turns": args.turns,
               "calibration": calibrate(base)}
        print(json.dumps(env, ensure_ascii=False), flush=True)
        rows = asyncio.run(run_all(args, base))
        summary = summarize(rows)
        judged = verdicts(summary)
        report(summary, judged)
        if out:
            out.write_text(json.dumps({"environment": env, "rows": rows, "verdicts": judged}, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
        return 0
    finally:
        clean_up(base)


if __name__ == "__main__":
    sys.exit(main())
