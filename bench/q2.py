"""Q2：同一任务、同一模型下，A、B 两种配置配对运行。

每一对试验紧挨着跑，谁先跑由种子决定：供应商的负载和模型版本随时间漂移，配对要尽量抵消这一点。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from flywhl_harness import Harness, HarnessConfig, Store
from flywhl_harness.events import E
from flywhl_harness.llm.base import Provider
from flywhl_harness.observe.trace import dumps, export_trajectory

from .common import Ledger, append_jsonl, read_jsonl
from .taskset import Task, load_tasks, prepare, render_prompt, run_check, served

IDENTITY = {"GIT_AUTHOR_NAME": "bench", "GIT_AUTHOR_EMAIL": "bench@example.invalid",
            "GIT_COMMITTER_NAME": "bench", "GIT_COMMITTER_EMAIL": "bench@example.invalid"}


def schedule(tasks: list[Task], trials: int, seed: int) -> list[tuple[Task, int, str]]:
    """全部试验的顺序。配置名固定是 A、B，每对的先后由种子决定。"""
    rng = random.Random(seed)
    order = []
    for task in tasks:
        for trial in range(1, trials + 1):
            pair = ["A", "B"]
            rng.shuffle(pair)
            order += [(task, trial, c) for c in pair]
    return order


@contextmanager
def trial_env(base: Path, port: int | None) -> Iterator[Path]:
    """每次试验一个干净的家目录。git 身份走环境变量：写进配置文件的话，沙箱开和关的差异会混进别的东西。"""
    home = base / "user-home"     # 不叫 home：/work/x/home/ 会撞上脱敏的路径规则，而那条规则宁可错杀
    home.mkdir()
    saved = {k: os.environ.get(k) for k in ("HOME", "FLYWHL_HOME", "BENCH_HOME", "BENCH_PORT", *IDENTITY)}
    os.environ.update({"HOME": str(home), "FLYWHL_HOME": str(base / "flywhl-home"), "BENCH_HOME": str(home),
                       "BENCH_PORT": str(port or ""), **IDENTITY})
    try:
        yield home
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _usage(rec: Any) -> dict[str, Any]:
    u = rec.usage or {}
    return {"input_tokens": int(u.get("input_tokens", 0)), "output_tokens": int(u.get("output_tokens", 0)),
            "cache_read_tokens": int(u.get("cache_read_tokens", 0)), "cost_usd": float(u.get("cost_usd") or 0.0)}


async def run_trial(task: Task, config_name: str, trial: int, plan: dict[str, Any], *, provider: Provider | None = None,
                    model: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """跑一次试验，返回结果行和轨迹。``provider`` 只有彩排时才给，正式运行用方案里的真实模型。"""
    q2 = plan["q2"]
    settings = q2["configs"][config_name]
    base = Path(tempfile.mkdtemp(prefix=f"bench-{task.id}-", dir=os.environ.get("BENCH_WORK")))
    try:
        ws = base / "ws"
        ws.mkdir()
        with served(task) as port, trial_env(base, port) as home:
            prepare(task, ws)
            cfg = HarnessConfig.load(path=base / "no-such-config.toml", workspace=str(ws))
            cfg.model = q2["model"]
            cfg.fallback_models = []
            cfg.permissions.on_ask = "allow"     # 无人值守。能拆掉沙箱的 outside_sandbox 仍然被拒绝，人工门不看这个设置
            cfg.permissions.sandbox = settings["sandbox"]
            cfg.permissions.sandbox_network = settings["sandbox_network"]
            cfg.budget.max_turns = q2["max_turns"]
            cfg.budget.max_cost_usd = q2["max_cost_usd"]
            kw = {"provider": provider, "model": model} if provider else {}
            h = Harness(cfg, store=Store(base / "harness.db", durability=cfg.durability), **kw)
            started = time.monotonic()
            timed_out = False
            async with h:
                s = h.create_session(task.title)
                run = await h.start_run(s.id, render_prompt(task, port=port, home=home))
                try:
                    async with asyncio.timeout(q2["timeout_seconds"]):
                        rec = await h.wait(run.id)
                except TimeoutError:
                    timed_out = True
                    await h.cancel(run.id, "超过基准的墙钟上限", by="bench")
                    rec = await h.wait(run.id)
                seconds = round(time.monotonic() - started, 2)
                traj = json.loads(dumps(export_trajectory(h.store, run.id)))
                events = h.store.list_events(run.id)
            passed, note = run_check(task, ws, home=home, port=port)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    row = {"task": task.id, "category": task.category, "sandbox_sensitive": task.sandbox_sensitive, "config": config_name,
           "trial": trial, "model": model or q2["model"], "status": "timeout" if timed_out else rec.status, "passed": passed,
           "note": note, "turns": sum(1 for e in events if e.type == E.TURN_START),
           "tool_calls": sum(1 for e in events if e.type == E.TOOL_START), "seconds": seconds, "run_id": run.id,
           "reason": (rec.result or {}).get("reason", ""), **_usage(rec)}
    return row, traj


def done_keys(out: Path) -> set[tuple[str, int, str]]:
    return {(r["task"], r["trial"], r["config"]) for r in read_jsonl(out / "q2" / "results.jsonl")}


async def run_q2(plan: dict[str, Any], out: Path, *, tasks: list[Task] | None = None, provider_for=None,
                 model: str | None = None) -> None:
    """``provider_for(task)`` 只有彩排时才给。结果逐行追加，中断后接着跑。"""
    q2 = plan["q2"]
    ledger = Ledger.from_plan(plan, out)
    done = done_keys(out)
    for task, trial, name in schedule(tasks or load_tasks(), q2["trials"], q2["seed"]):
        if (task.id, trial, name) in done:
            continue
        ledger.check(q2["model"])
        provider = provider_for(task) if provider_for else None
        row, traj = await run_trial(task, name, trial, plan, provider=provider, model=model)
        append_jsonl(out / "q2" / "results.jsonl", [row])
        append_jsonl(out / "q2" / "trajectories.jsonl", [{"task": task.id, "config": name, "trial": trial, "trajectory": traj}])
        ledger.charge(q2["model"], row["cost_usd"])
        print(f"Q2 {task.id} {name} #{trial}：{'过' if row['passed'] else '败'} {row['status']} {row['seconds']} 秒 "
              f"${row['cost_usd']:.4f}", flush=True)
