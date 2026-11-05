#!/usr/bin/env python3
"""耐久浸泡测试：长时间并发 SSE 看资源有没有涨，反复 kill -9 看副作用是不是恰好一次。

    uv run python scripts/soak.py sse --seconds 7200 --json soak-sse.json
    uv run python scripts/soak.py kill9 --cycles 50 --json soak-kill9.json

两段都不联网、不用真实模型。Agent 只跑 echo、touch 这类哨兵命令，只碰本脚本建的临时目录。
"""

from __future__ import annotations

import os
import shutil
import signal
import statistics
import subprocess
import tempfile
from collections import Counter
from itertools import pairwise
from pathlib import Path
from typing import Any

PREFIX = "flywhl-soak-"
WARMUP = 0.2            # 判定时跳过的预热比例
FD_SEGMENT = 0.25       # 句柄数的首段、末段各占预热之后样本的比例
MIN_SAMPLES = 4         # 预热之后样本少于这个数，算不出趋势
TWO_HOURS = 7200.0


# ---------------------------------------------------------------- 判定：只看数据，不碰进程
def after_warmup(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return samples[int(len(samples) * WARMUP):]


def rss_verdict(samples: list[dict[str, Any]], limit_mib: float) -> dict[str, Any]:
    """线性拟合预热之后的 RSS，换算成再跑 2 小时会涨多少 MiB。"""
    rest = after_warmup(samples)
    if len(rest) < MIN_SAMPLES:
        return {"passed": False, "reason": f"预热之后只有 {len(rest)} 个样本，算不出增长率"}
    fit = statistics.linear_regression([s["t"] for s in rest], [s["rss_mib"] for s in rest])
    growth = fit.slope * TWO_HOURS
    return {"passed": growth <= limit_mib, "growth_mib_per_2h": round(growth, 2), "limit_mib": limit_mib,
            "samples": len(rest), "first_mib": rest[0]["rss_mib"], "last_mib": rest[-1]["rss_mib"]}


def fd_verdict(samples: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    """句柄数看末段中位数比首段多出多少。用中位数是因为采样可能正好撞上临时打开的数据库和管道。"""
    rest = after_warmup(samples)
    if len(rest) < MIN_SAMPLES:
        return {"passed": False, "reason": f"预热之后只有 {len(rest)} 个样本，算不出增长"}
    n = max(1, int(len(rest) * FD_SEGMENT))
    head = statistics.median(s["fds"] for s in rest[:n])
    tail = statistics.median(s["fds"] for s in rest[-n:])
    return {"passed": tail - head <= limit, "growth": tail - head, "limit": limit, "head_median": head, "tail_median": tail}


def seq_problem(seqs: list[int]) -> str | None:
    """一个运行的事件序号从 1 开始逐个递增。ephemeral 事件也落库，订阅晚了也不该有空洞。"""
    if not seqs:
        return "没有收到事件"
    if seqs[0] != 1:
        return f"seq 从 {seqs[0]} 开始"
    for prev, cur in pairwise(seqs):
        if cur != prev + 1:
            return f"seq 从 {prev} 跳到 {cur}"
    return None


def ledger_verdict(lines: list[str], expected: list[str]) -> dict[str, Any]:
    """账本每行以幂等键开头。每个键恰好一行：多了是副作用重复，少了是副作用丢失，不认识的键是别处写进来的。"""
    counts = Counter(line.split("\t", 1)[0] for line in lines if line.strip())
    duplicated = sorted(k for k, c in counts.items() if c > 1)
    missing = sorted(k for k in expected if counts[k] == 0)
    unknown = sorted(set(counts) - set(expected))
    return {"passed": not (duplicated or missing or unknown), "expected": len(expected), "lines": sum(counts.values()),
            "duplicated": duplicated, "missing": missing, "unknown": unknown}


def kill_phase(step_status: str | None) -> str:
    """kill 落在哪个阶段，看现场的 step：没有 step 是 intent 之前，started 是 intent 和结果之间，其余是结果之后。"""
    if step_status is None:
        return "before_intent"
    return "between_intent_and_result" if step_status == "started" else "after_result"


def cycle_problems(status: str, versions: list[int], steps: int, takeover: bool, dead: str | None, final: str | None) -> list[str]:
    """一轮接管结束后要成立的不变量。``takeover`` 表示 kill 时运行还没结束，需要新进程接管。"""
    out = []
    if status != "completed":
        out.append(f"最终状态是 {status}")
    if any(a >= b for a, b in pairwise(versions)):
        out.append(f"版本号不单调：{versions}")
    if steps != 1:
        out.append(f"有 {steps} 个工具步骤，应该只有 1 个")
    if takeover and (final is None or final == dead):
        out.append(f"接管者还是 {final}")
    return out


def percentile(values: list[float], q: int) -> float:
    return statistics.quantiles(values, n=100, method="inclusive")[q - 1] if len(values) > 1 else values[0]


def latency(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p50_s": None, "p99_s": None, "max_s": None}
    return {"p50_s": round(percentile(values, 50), 3), "p99_s": round(percentile(values, 99), 3), "max_s": round(max(values), 3)}


# ---------------------------------------------------------------- 安全：临时目录和子进程
def make_base() -> Path:
    return Path(tempfile.mkdtemp(prefix=PREFIX)).resolve()


def clean_up(base: Path) -> None:
    """只删 make_base 建的目录：直接在系统临时目录下、名字带固定前缀、不是软链接，有一条不符就拒绝。"""
    root = Path(tempfile.gettempdir()).resolve()
    if base.is_symlink() or not base.name.startswith(PREFIX) or base.resolve().parent != root:
        raise ValueError(f"拒绝清理 {base}：不是 {root} 下以 {PREFIX} 开头的目录")
    shutil.rmtree(base)


_spawned: dict[int, subprocess.Popen] = {}


def spawn(cmd: list[str], **kwargs: Any) -> subprocess.Popen:
    """所有子进程都从这里启动，记下 pid，发信号时用来核对。"""
    proc = subprocess.Popen(cmd, **kwargs)
    _spawned[proc.pid] = proc
    return proc


def is_own_child(pid: int) -> bool:
    out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True)
    return out.stdout.strip() == str(os.getpid())


def kill9(proc: subprocess.Popen) -> bool:
    """只对自己启动的子进程发 SIGKILL。进程已经退出就不发（pid 可能已被复用），返回值表示信号有没有发出去。

    没有回收的子进程哪怕已经退出也占着 pid，所以 poll 返回 None 之后这个 pid 不会变成别的进程。
    """
    if _spawned.get(proc.pid) is not proc:
        raise RuntimeError(f"pid {proc.pid} 不是本脚本启动的，拒绝发信号")
    if proc.poll() is not None:
        return False
    if not is_own_child(proc.pid):
        raise RuntimeError(f"pid {proc.pid} 已经不是本脚本的子进程，拒绝发信号")
    os.kill(proc.pid, signal.SIGKILL)
    return True
