#!/usr/bin/env python3
"""耐久浸泡测试：长时间并发 SSE 看资源有没有涨，反复 kill -9 看副作用是不是恰好一次。

    uv run python scripts/soak.py sse --seconds 7200 --json soak-sse.json
    uv run python scripts/soak.py kill9 --cycles 50 --json soak-kill9.json

两段都不联网、不用真实模型。Agent 只跑 echo、touch 这类哨兵命令，只碰本脚本建的临时目录。

sse 启动真实的 flywhl serve，几个会话并发地发起运行并消费 SSE，周期性采样服务进程的 RSS、句柄数、线程数和数据库大小。
运行时长的分位数只记录，不做判定。

kill9 循环：worker 跑一个带副作用的运行（工具往账本追加一行），随机时间点 kill -9，新进程接管到结束，
再核对每个幂等键在账本里恰好一行。kill 落点多数是完全随机的，每 3 轮瞄准一次工具窗口，短跑也能覆盖到 intent 和结果之间。

子进程的 HOME、TMPDIR、FLYWHL_HOME、工作目录、数据目录都在一个临时基目录里，结束时只删这一个目录。
子进程的环境变量走白名单，供应商密钥和代理进不去。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import os
import platform
import queue
import random
import secrets
import shutil
import signal
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx

from flywhl_harness import Effect, Harness, HarnessConfig, RunStatus, ScriptedProvider, Tool, ToolContext, ToolResult
from flywhl_harness.runtime.state import TERMINAL

PREFIX = "flywhl-soak-"
WARMUP = 0.2            # 判定时跳过的预热比例
FD_SEGMENT = 0.25       # 句柄数的首段、末段各占预热之后样本的比例
MIN_SAMPLES = 4         # 预热之后样本少于这个数，算不出趋势
TWO_HOURS = 7200.0
RUN_TIMEOUT = 120.0     # 单个运行从发起到结束的上限
READ_TIMEOUT = 60.0     # 服务端空闲时每 15 秒发一次 ping，读超时说明事件循环卡死或连接断了
MAX_FAILURES = 20       # 失败这么多次就提前结束，不必空转到时长用完
LEDGER_TOOL = "ledger_append"
HEARTBEAT, STALE_AFTER = 0.2, 1.0       # 心跳过期之前 recover 不会抢运行，这决定了每轮至少要等多久
TOOL_BEFORE, TOOL_AFTER = 0.15, 0.15    # 工具里写账本前后各等一会儿，这两段就是 intent 和结果之间的窗口
MODEL_PAUSE = 0.15      # 两个模型回合开始前各等一会儿，撑开 intent 之前和结果之后的窗口
KILL_WINDOW = 0.75      # 随机 kill 的时间窗，从运行创建开始算，覆盖一个运行的全程
AIM_EVERY = 3           # 每隔几轮瞄准一次工具窗口
CYCLE_TIMEOUT = 60.0    # 单个 worker 的上限
ROOT = Path(__file__).resolve().parents[1]
ENV_KEEP = {"PATH", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "SYSTEMROOT", "COMSPEC", "PATHEXT"}


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


def stop(proc: subprocess.Popen, grace: float = 15.0) -> None:
    """先 SIGTERM 等它自己退出，等不到再 kill -9。"""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(grace)
    except subprocess.TimeoutExpired:
        kill9(proc)
        proc.wait()


def child_env(base: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ENV_KEEP or k.startswith("LC_")}
    env.update(HOME=str(base / "home"), TMPDIR=str(base / "tmp"), FLYWHL_HOME=str(base / "flywhl-home"),
               PYTHONPATH=str(ROOT / "src"))
    return env


def prepare(base: Path, durability: str, **settings: Any) -> Path:
    """建好子进程要用的目录，写配置文件。-c 显式指定的配置视为可信，on_ask = "allow" 才生效。"""
    for name in ("home", "tmp", "flywhl-home", "workspace", "data"):
        (base / name).mkdir()
    lines = ['model = "mock"', f'data_dir = {json.dumps(str(base / "data"))}']
    lines += [f"{key} = {json.dumps(value)}" for key, value in settings.items()]
    if durability != "default":
        lines.append(f"durability = {json.dumps(durability)}")
    lines += ["", "[permissions]", 'on_ask = "allow"', ""]
    config = base / "flywhl.toml"
    config.write_text("\n".join(lines), encoding="utf-8")
    return config


def durability_setting(requested: str | None) -> str:
    """配置项不存在时写进配置文件会报“未知配置项”，所以先探测。没有这一项就忽略，结果里记 default。"""
    if requested is None:
        return "default"
    if "durability" not in {f.name for f in dataclasses.fields(HarnessConfig)}:
        print("当前版本没有 durability 配置项，忽略 --durability，按默认设置运行", file=sys.stderr)
        return "default"
    return requested


def environment() -> dict[str, str]:
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
    return {"platform": platform.platform(), "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
            "commit": commit.stdout.strip()}


def log_tail(path: Path, chars: int = 2000) -> str:
    return path.read_text(encoding="utf-8", errors="replace")[-chars:] if path.exists() else ""


# ---------------------------------------------------------------- sse：长时间并发
def rss_mib(pid: int) -> float:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, check=True)
    return round(int(out.stdout) / 1024, 2)


def fd_count(pid: int) -> int:
    if Path("/proc/self/fd").is_dir():
        return len(os.listdir(f"/proc/{pid}/fd"))
    out = subprocess.run(["lsof", "-nP", "-p", str(pid), "-F", "f"], capture_output=True, text=True)
    return sum(1 for line in out.stdout.splitlines() if line[:1] == "f" and line[1:].isdigit())     # 跳过 cwd、txt 这些非句柄条目


def thread_count(pid: int) -> int:
    if Path("/proc/self/task").is_dir():
        return len(os.listdir(f"/proc/{pid}/task"))
    out = subprocess.run(["ps", "-M", "-p", str(pid)], capture_output=True, text=True, check=True)
    return len(out.stdout.splitlines()) - 1


def file_mib(path: Path) -> float:
    return round(path.stat().st_size / 1048576, 2) if path.exists() else 0.0


def sample(pid: int, db: Path, t: float) -> dict[str, Any]:
    return {"t": round(t, 1), "rss_mib": rss_mib(pid), "fds": fd_count(pid), "threads": thread_count(pid),
            "db_mib": file_mib(db), "wal_mib": file_mib(db.with_name(db.name + "-wal"))}


class Tally:
    def __init__(self) -> None:
        self.runs, self.events, self.failed = 0, 0, 0
        self.durations: list[float] = []
        self.failures: list[str] = []

    def fail(self, where: str, why: str) -> None:
        self.failed += 1
        if len(self.failures) < MAX_FAILURES:
            self.failures.append(f"{where}：{why}")
            print(f"失败 {where}：{why}", flush=True)

    @property
    def give_up(self) -> bool:
        return self.failed >= MAX_FAILURES


async def read_events(client: httpx.AsyncClient, rid: str) -> tuple[list[int], list[dict[str, Any]]]:
    """消费一个运行的 SSE 直到 stream_end，返回收到的序号和工具结果。没等到 stream_end 就断了算 SSE 中断。"""
    seqs: list[int] = []
    tools: list[dict[str, Any]] = []
    async with client.stream("GET", f"/api/runs/{rid}/events") as resp:
        resp.raise_for_status()
        kind = ""
        async for line in resp.aiter_lines():
            if line.startswith("id: "):
                seqs.append(int(line[4:]))
            elif line.startswith("event: "):
                kind = line[7:]
                if kind == "stream_end":
                    return seqs, tools
            elif kind == "tool_end" and line.startswith("data: "):
                tools.append(json.loads(line[6:])["data"])
    raise ConnectionError("连接在 stream_end 之前结束")


async def one_run(client: httpx.AsyncClient, sid: str, command: str) -> tuple[float, int, list[str]]:
    """发起一个运行并消费到结束，返回 (耗时, 事件数, 问题列表)。"""
    started = time.perf_counter()
    resp = await client.post(f"/api/sessions/{sid}/runs", json={"prompt": f"运行 `{command}`"})
    resp.raise_for_status()
    rid = resp.json()["id"]
    seqs, tools = await read_events(client, rid)
    took = time.perf_counter() - started
    resp = await client.get(f"/api/runs/{rid}")
    resp.raise_for_status()
    run = resp.json()
    problems = []
    if gap := seq_problem(seqs):
        problems.append(f"{rid} {gap}")
    if run["status"] != "completed":
        problems.append(f"{rid} 状态 {run['status']}")
    if len(tools) != 1 or tools[0].get("is_error"):     # 工具没真的跑成，运行完成也不算数
        problems.append(f"{rid} 工具结果不对：{[(t.get('name'), t.get('status')) for t in tools]}")
    return took, len(seqs), problems


async def session_loop(client: httpx.AsyncClient, index: int, deadline: float, tally: Tally, workspace: Path) -> None:
    resp = await client.post("/api/sessions", json={"title": f"浸泡 {index}"})
    resp.raise_for_status()
    sid = resp.json()["id"]
    n = 0
    while time.monotonic() < deadline and not tally.give_up:
        n += 1
        marker = workspace / f"soak-{index}-{n}.txt"
        command = f"touch {marker.name}" if n % 2 == 0 else f"echo soak-{index}-{n}"
        try:
            async with asyncio.timeout(RUN_TIMEOUT):
                took, events, problems = await one_run(client, sid, command)
        except (httpx.HTTPError, ConnectionError, TimeoutError) as e:
            tally.fail(f"会话 {index} 第 {n} 个运行", f"{type(e).__name__}：{e}")
            await asyncio.sleep(1.0)
            continue
        if n % 2 == 0:
            if not marker.exists():
                problems.append(f"{marker.name} 没有创建出来，touch 没有真的执行")
            marker.unlink(missing_ok=True)   # 工作区保持很小，影子快照的开销不随时长增长
        tally.runs += 1
        tally.events += events
        tally.durations.append(took)
        for problem in problems:
            tally.fail(f"会话 {index} 第 {n} 个运行", problem)


async def sample_loop(proc: subprocess.Popen, db: Path, every: float, started: float, deadline: float,
                      tally: Tally, samples: list[dict[str, Any]]) -> None:
    while True:
        if proc.poll() is not None:
            tally.fail("服务进程", f"已退出，退出码 {proc.returncode}")
            return
        row = await asyncio.to_thread(sample, proc.pid, db, time.monotonic() - started)
        samples.append(row)
        print(f"t={row['t']:.0f}s rss={row['rss_mib']}MiB fds={row['fds']} threads={row['threads']} "
              f"db={row['db_mib']}MiB wal={row['wal_mib']}MiB runs={tally.runs} failed={tally.failed}", flush=True)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or tally.give_up:
            return
        await asyncio.sleep(min(every, remaining))


async def wait_ready(client: httpx.AsyncClient, proc: subprocess.Popen, timeout: float = 30.0) -> None:
    """等服务就绪，并确认答话的是这个进程（令牌对得上）、用的是 mock provider。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"flywhl serve 启动失败，退出码 {proc.returncode}")
        try:
            resp = await client.get("/api/info")
        except httpx.TransportError:
            await asyncio.sleep(0.1)
            continue
        resp.raise_for_status()
        if resp.json()["provider"] != "mock":
            raise RuntimeError(f"服务用的不是 mock provider：{resp.json()['provider']}")
        return
    raise RuntimeError(f"flywhl serve 在 {timeout:.0f} 秒内没有就绪")


async def drive(args: argparse.Namespace, proc: subprocess.Popen, port: int, token: str, base: Path, tally: Tally,
                samples: list[dict[str, Any]]) -> None:
    db = base / "data" / "harness.db"
    limits = httpx.Timeout(10.0, read=READ_TIMEOUT)
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers={"Authorization": f"Bearer {token}"},
                                 timeout=limits, trust_env=False) as client:
        await wait_ready(client, proc)
        started = time.monotonic()
        deadline = started + args.seconds
        loops = [asyncio.create_task(session_loop(client, i, deadline, tally, base / "workspace")) for i in range(args.sessions)]
        await sample_loop(proc, db, args.sample_seconds, started, deadline, tally, samples)
        for result in await asyncio.gather(*loops, return_exceptions=True):
            if isinstance(result, Exception):
                tally.fail("会话循环", f"{type(result).__name__}：{result}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve_cmd(base: Path, config: Path, port: int, token: str) -> list[str]:
    # 令牌必须用 --token=值 的写法：urlsafe 令牌可能以 - 开头，分开写时 argparse 会把它当成选项
    return [sys.executable, "-m", "flywhl_harness.cli", "serve", "--host", "127.0.0.1", "--port", str(port), f"--token={token}",
            "--trust", "--provider", "mock", "-w", str(base / "workspace"), "-c", str(config)]


def run_sse(args: argparse.Namespace, base: Path) -> dict[str, Any]:
    durability = durability_setting(args.durability)
    config = prepare(base, durability)
    token, port = secrets.token_urlsafe(32), free_port()
    tally, samples = Tally(), []
    log_path = base / "serve.log"
    with open(log_path, "wb") as log:
        proc = spawn(serve_cmd(base, config, port, token), cwd=base / "workspace", env=child_env(base),
                     stdout=log, stderr=subprocess.STDOUT)
        print(f"flywhl serve pid {proc.pid}，端口 {port}，{args.sessions} 个会话，{args.seconds:.0f} 秒", flush=True)
        try:
            asyncio.run(drive(args, proc, port, token, base, tally, samples))
        except Exception as e:
            tally.fail("驱动", f"{type(e).__name__}：{e}")
        finally:
            stop(proc)
    if tally.failed:
        print(log_tail(log_path).replace(token, "***"), file=sys.stderr)     # 启动日志里有带令牌的链接
    rss, fds = rss_verdict(samples, args.max_rss_growth_mib), fd_verdict(samples, args.max_fd_growth)
    return {"segment": "sse", "durability": durability, "environment": environment(), "sessions": args.sessions,
            "seconds": args.seconds, "sample_seconds": args.sample_seconds,
            "runs": tally.runs, "events": tally.events, "failed": tally.failed, "failures": tally.failures,
            "run_seconds": latency(tally.durations), "rss": rss, "fds": fds, "samples": samples,
            "passed": rss["passed"] and fds["passed"] and tally.failed == 0 and tally.runs > 0}


def describe_rss(rss: dict[str, Any]) -> str:
    if "reason" in rss:
        return rss["reason"]
    return f"{rss['first_mib']} → {rss['last_mib']} MiB，折合 2 小时涨 {rss['growth_mib_per_2h']} MiB（上限 {rss['limit_mib']}）"


def describe_fds(fds: dict[str, Any]) -> str:
    if "reason" in fds:
        return fds["reason"]
    return f"{fds['head_median']} → {fds['tail_median']}，多出 {fds['growth']}（上限 {fds['limit']}）"


def report_sse(result: dict[str, Any]) -> None:
    took = result["run_seconds"]
    print(f"\n运行 {result['runs']} 个，事件 {result['events']} 条，失败 {result['failed']} 次，durability = {result['durability']}")
    if result["runs"]:
        print(f"运行耗时（只记录）：p50 {took['p50_s']}s，p99 {took['p99_s']}s，最大 {took['max_s']}s")
    print(f"RSS：{describe_rss(result['rss'])}")
    print(f"句柄：{describe_fds(result['fds'])}")
    for failure in result["failures"]:
        print(f"失败：{failure}")
    print(f"结论：{'通过' if result['passed'] else '不通过'}")


# ---------------------------------------------------------------- kill9：接管循环
def ledger_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def has_entry(lines: list[str], key: str) -> bool:
    return any(line.startswith(f"{key}\t") for line in lines)


def announce(**fields: Any) -> None:
    print(json.dumps(fields), flush=True)


class LedgerTool(Tool):
    """外部副作用：往账本追加一行。execute 故意不按幂等键去重，harness 重复执行就会在账本里留下两行。"""

    name = LEDGER_TOOL
    description = "往账本追加一行记录"
    parameters = {"type": "object", "properties": {"entry": {"type": "string"}}, "required": ["entry"], "additionalProperties": False}
    effect = Effect.EXTERNAL
    interruptible = False
    idempotent = True       # reconcile 查不到就说明确实没写过，可以用同一幂等键重做

    def __init__(self, path: Path):
        self.path = path

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        announce(tool="started")
        await asyncio.sleep(TOOL_BEFORE)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, f"{ctx.idempotency_key}\t{args['entry']}\t{os.getpid()}\n".encode())
        finally:
            os.close(fd)
        await asyncio.sleep(TOOL_AFTER)
        return ToolResult(content=f"已记账：{args['entry']}")

    async def reconcile(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult | None:
        if has_entry(ledger_lines(self.path), ctx.idempotency_key):
            return ToolResult(content=f"[对账] 账本里已有：{args['entry']}")
        return None


async def worker(args: argparse.Namespace) -> int:
    """worker 子进程。start 发起运行，recover 接管心跳过期的运行。两边的模型脚本和工具一样，才接得上。"""
    base = Path(args.base)
    config = HarnessConfig.load(path=base / "flywhl.toml", workspace=str(base / "workspace"))
    script = [{"text": "先记一笔账。", "delay_before": MODEL_PAUSE, "tool_calls": [{"name": LEDGER_TOOL, "args": {"entry": args.entry}}]},
              {"text": "账本已经写好。", "delay_before": MODEL_PAUSE}]
    h = Harness(config, provider=ScriptedProvider(script, chunk=8, delay=0.01), model="scripted")
    h.tools.register(LedgerTool(base / "ledger.log"))
    async with h:
        if args.mode == "start":
            run = await h.start_run(h.create_session(args.entry).id, f"记账 {args.entry}")
            announce(run_id=run.id)
            await h.wait(run.id)
        else:
            for rid in await h.recover(auto_resume=True):
                await h.wait(rid)
    return 0


def worker_cmd(base: Path, mode: str, entry: str) -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), "_worker", mode, "--base", str(base), "--entry", entry]


def read_lines(proc: subprocess.Popen) -> queue.Queue:
    """管道读取放在线程里，主线程才能带超时地等下一行。"""
    lines: queue.Queue = queue.Queue()

    def pump() -> None:
        with proc.stdout:  # type: ignore[union-attr]
            for line in proc.stdout:  # type: ignore[union-attr]
                lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    return lines


def next_message(lines: queue.Queue) -> dict[str, Any]:
    line = lines.get(timeout=CYCLE_TIMEOUT)
    if line is None:
        raise RuntimeError("worker 提前退出")
    return json.loads(line)


def read_run(db: Path, rid: str) -> dict[str, Any]:
    """只读连接看库：不写库也不做检查点，接管进程看到的还是崩溃时的原样。"""
    with contextlib.closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as c:
        status, worker_id = c.execute("SELECT status, worker_id FROM runs WHERE id=?", (rid,)).fetchone()
        steps = c.execute("SELECT status, idem_key FROM steps WHERE run_id=?", (rid,)).fetchall()
        moves = c.execute("SELECT data FROM events WHERE run_id=? AND type='status' ORDER BY seq", (rid,)).fetchall()
    return {"status": status, "worker": worker_id, "steps": steps, "versions": [json.loads(data)["version"] for (data,) in moves]}


def run_cycle(index: int, base: Path, env: dict[str, str], rng: random.Random) -> dict[str, Any]:
    entry = f"cycle-{index:04d}"
    db, ledger, workspace = base / "data" / "harness.db", base / "ledger.log", base / "workspace"
    with open(base / "worker.log", "ab") as log:
        proc = spawn(worker_cmd(base, "start", entry), cwd=workspace, env=env, stdout=subprocess.PIPE, stderr=log, text=True)
        try:
            lines = read_lines(proc)
            rid = next_message(lines)["run_id"]
            aimed = index % AIM_EVERY == 0
            if aimed:
                next_message(lines)     # 工具开始执行时 intent 已经落库
            delay = rng.uniform(0, TOOL_BEFORE + TOOL_AFTER if aimed else KILL_WINDOW)
            time.sleep(delay)
        except BaseException:
            kill9(proc)
            raise
        killed = kill9(proc)
        proc.wait()

        scene = read_run(db, rid)
        step = scene["steps"][0] if scene["steps"] else (None, None)
        phase = kill_phase(step[0]) if killed else "finished_before_kill"
        written = step[1] is not None and has_entry(ledger_lines(ledger), step[1])
        takeover = RunStatus(scene["status"]) not in TERMINAL
        problems: list[str] = []
        if takeover:
            time.sleep(STALE_AFTER + 0.3)       # 心跳过期之前，recover 不会抢一个可能还活着的 worker
            recover = spawn(worker_cmd(base, "recover", entry), cwd=workspace, env=env, stdout=subprocess.DEVNULL, stderr=log)
            try:
                if recover.wait(CYCLE_TIMEOUT):
                    problems.append(f"recover 退出码 {recover.returncode}")
            except subprocess.TimeoutExpired:
                kill9(recover)
                recover.wait()
                problems.append(f"recover {CYCLE_TIMEOUT:.0f} 秒没有结束")
    final = read_run(db, rid)
    problems += cycle_problems(final["status"], final["versions"], len(final["steps"]), takeover, scene["worker"], final["worker"])
    return {"entry": entry, "run_id": rid, "aimed": aimed, "delay_ms": round(delay * 1000), "killed": killed, "phase": phase,
            "scene_status": scene["status"], "effect_written": written, "takeover": takeover, "final_status": final["status"],
            "final_step": final["steps"][0][0] if final["steps"] else None, "keys": [key for _, key in final["steps"]],
            "versions": final["versions"], "problems": problems}


def run_kill9(args: argparse.Namespace, base: Path) -> dict[str, Any]:
    durability = durability_setting(args.durability)
    prepare(base, durability, heartbeat_seconds=HEARTBEAT, stale_after_seconds=STALE_AFTER, snapshots=False)
    seed = secrets.randbits(32) if args.seed is None else args.seed
    rng, env = random.Random(seed), child_env(base)
    cycles = []
    for index in range(args.cycles):
        try:
            record = run_cycle(index, base, env, rng)
        except (RuntimeError, ValueError, TypeError, queue.Empty, sqlite3.Error) as e:
            record = {"entry": f"cycle-{index:04d}", "phase": "unknown", "keys": [], "problems": [f"{type(e).__name__}：{e}"]}
        cycles.append(record)
        print(f"[{index + 1}/{args.cycles}] {record['phase']} → {record.get('final_status')}（{record.get('final_step')}）"
              + (f" 问题：{record['problems']}" if record["problems"] else ""), flush=True)
    problems = [f"{c['entry']}：{p}" for c in cycles for p in c["problems"]]
    if problems:
        print(log_tail(base / "worker.log"), file=sys.stderr)
    ledger = ledger_verdict(ledger_lines(base / "ledger.log"), [key for c in cycles for key in c["keys"]])
    phases = Counter(c["phase"] for c in cycles)
    between_written = sum(1 for c in cycles if c["phase"] == "between_intent_and_result" and c["effect_written"])
    return {"segment": "kill9", "durability": durability, "environment": environment(), "seed": seed, "cycles": len(cycles),
            "phases": dict(phases), "between_effect_written": between_written,
            "takeovers": sum(1 for c in cycles if c.get("takeover")),
            "reconciled": sum(1 for c in cycles if c.get("final_step") == "reconciled"),
            "covered_between": phases["between_intent_and_result"] > 0, "ledger": ledger, "problems": problems, "records": cycles,
            "passed": bool(cycles) and ledger["passed"] and not problems}


def report_kill9(result: dict[str, Any]) -> None:
    phases, ledger = result["phases"], result["ledger"]
    print(f"\n{result['cycles']} 轮，接管 {result['takeovers']} 次，其中对账收尾 {result['reconciled']} 次，"
          f"durability = {result['durability']}，seed = {result['seed']}")
    print(f"kill 落点：intent 之前 {phases.get('before_intent', 0)}，intent 和结果之间 {phases.get('between_intent_and_result', 0)}"
          f"（其中账本已写 {result['between_effect_written']}），结果之后 {phases.get('after_result', 0)}，"
          f"kill 前已结束 {phases.get('finished_before_kill', 0)}")
    if not result["covered_between"]:
        print("未覆盖到 intent 和结果之间：这次没有验证到崩溃窗口")
    print(f"账本：{ledger['lines']} 行，应有 {ledger['expected']} 行；"
          f"重复 {ledger['duplicated']}，缺失 {ledger['missing']}，不认识 {ledger['unknown']}")
    for problem in result["problems"]:
        print(f"失败：{problem}")
    print(f"结论：{'通过' if result['passed'] else '不通过'}")


# ---------------------------------------------------------------- 入口
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--durability", choices=("full", "normal"), help="会话库落盘强度。当前版本没有这个配置项时忽略")
    common.add_argument("--json", help="把结果写成 JSON 文件")
    sub = ap.add_subparsers(dest="segment", required=True, metavar="{sse,kill9}")
    sse = sub.add_parser("sse", parents=[common], help="长时间并发：资源增长和 SSE 完整性")
    sse.add_argument("--seconds", type=float, default=TWO_HOURS)
    sse.add_argument("--sessions", type=int, default=4)
    sse.add_argument("--sample-seconds", type=float, default=30.0)
    sse.add_argument("--max-rss-growth-mib", type=float, default=64.0, help="折合 2 小时的 RSS 增长上限")
    sse.add_argument("--max-fd-growth", type=int, default=8, help="句柄数末段中位数比首段多出的上限")
    kill = sub.add_parser("kill9", parents=[common], help="接管循环：kill -9 之后副作用恰好一次")
    kill.add_argument("--cycles", type=int, default=50)
    kill.add_argument("--seed", type=int, help="kill 时间点的随机种子，默认随机并记进结果")
    hidden = sub.add_parser("_worker")      # kill9 启动的子进程
    hidden.add_argument("mode", choices=("start", "recover"))
    hidden.add_argument("--base", required=True)
    hidden.add_argument("--entry", required=True)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.segment == "_worker":
        return asyncio.run(worker(args))
    out = Path(args.json).resolve() if args.json else None
    base = make_base()
    try:
        result = (run_sse if args.segment == "sse" else run_kill9)(args, base)
        (report_sse if args.segment == "sse" else report_kill9)(result)
        if out:     # 先写结果再清理：清理出问题也不能丢掉跑了几个小时的数据
            out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally:
        for proc in _spawned.values():
            stop(proc)
        clean_up(base)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
