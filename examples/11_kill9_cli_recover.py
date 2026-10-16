"""11 · 跨进程崩溃接管：flywhl run 在副作用工具执行中途被 kill -9，另一个 flywhl recover 进程接管并完成。

运行：python examples/11_kill9_cli_recover.py [--keep]

与 02 的区别：02 在同一个 Python 程序里用 API 演示；这里两端都是真实的 CLI 进程，只通过工作区里的 SQLite 交接，
用内置演示模型（mock），不需要 API Key，结果确定，可以放进 CI。结束时打印这次 Run 的完整状态迁移轨迹，
并逐条核对不变量，任何一条不成立就以非零状态退出。

时间线：
  进程 A：flywhl run → running → 调用 send_notification（外部副作用，带幂等键）
          → 预写 intent（step=started）→ 外部系统已记账 → 等回包的 1.5 秒窗口里被 kill -9
  现场：  run 仍是 running、心跳停止、step 只有 intent 没有结果
  进程 B：flywhl recover → 心跳过期 → CAS 迁移 running → interrupted → queued → running
          → 发现“有 intent 无 result”的调用 → 按幂等键向外部系统对账 → 补写结果，不重发 → completed
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from _common import B, D, G, R, X, say, temp_workspace, title

from flywhl_harness import Store
from flywhl_harness.events import E
from flywhl_harness.runtime.state import TRANSITIONS
from flywhl_harness.tools.demo import FakeNotifyService

STALE_AFTER = 2.0          # 心跳超过这么久没更新，才算 Worker 已死
CONFIG = f"""model = "mock"
enable_demo_tools = true
heartbeat_seconds = 0.5
stale_after_seconds = {STALE_AFTER}
snapshots = false

[permissions]
on_ask = "allow"
"""
PROMPT = "发送通知：v1.2.0 已上线"


def cli(ws: Path, *args: str) -> list[str]:
    # -c 显式指定的配置视为可信（on_ask = "allow" 属于需要信任的设置）
    return [sys.executable, "-m", "flywhl_harness.cli", *args, "-w", str(ws), "-c", str(ws / "flywhl.toml")]


def clean_env(ws: Path) -> dict[str, str]:
    """子进程环境：本机的 FLYWHL_* 配置、降级链与信任记录都不参与，结果只取决于这个脚本。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FLYWHL_", "LLM_"))}
    src = str(Path(__file__).resolve().parents[1] / "src")
    env.update(FLYWHL_HOME=str(ws / ".flywhl-home"), PYTHONPATH=os.pathsep.join(filter(None, [src, env.get("PYTHONPATH")])))
    return env


def crash(ws: Path, env: dict[str, str], svc: FakeNotifyService) -> str:
    """进程 A：启动 Run，等外部系统记账后、工具回包之前 kill -9。返回 run_id。"""
    proc = subprocess.Popen(cli(ws, "run", "--json", PROMPT), cwd=ws, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    run_id, deadline = None, time.monotonic() + 30
    for line in proc.stdout:  # type: ignore[union-attr]
        ev = json.loads(line) if line.startswith("{") else {}
        run_id = run_id or ev.get("run_id")
        if ev.get("type") == E.TOOL_START and ev["data"]["name"] == "send_notification":
            break
        if time.monotonic() > deadline:
            proc.kill()
            raise SystemExit("等不到 send_notification 开始执行")
    while svc.count() == 0 and time.monotonic() < deadline:   # 外部系统记账了，进程 A 还在等回包
        time.sleep(0.02)
    say(f"进程 A（pid {proc.pid}）：外部系统已记录通知，工具还没回包 → kill -9")
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()
    assert run_id, "没有拿到 run_id"
    return run_id


def show_scene(store: Store, run_id: str, svc: FakeNotifyService) -> None:
    run = store.get_run(run_id)
    assert run is not None
    age = time.time() - (run.heartbeat_at or 0)
    print(f"  run.status = {run.status}  worker = {run.worker_id}  心跳停在 {age:.1f}s 前  version = {run.version}")
    for s in store.list_steps(run_id):
        print(f"  step {s.tool:<18} status = {s.status:<10} effect = {s.effect:<8} 幂等键 = {s.idem_key}")
    print(f"  外部通知数 = {svc.count()}")


def trail(store: Store, run_id: str) -> list[tuple[str, str, str, int]]:
    return [(e.data["from"], e.data["to"], e.data.get("reason", ""), e.data.get("version", 0))
            for e in store.list_events(run_id) if e.type == E.STATUS]


def main() -> int:
    keep = "--keep" in sys.argv
    title("11 跨进程崩溃接管（flywhl run → kill -9 → flywhl recover）")
    ws = temp_workspace("11")
    (ws / "flywhl.toml").write_text(CONFIG, encoding="utf-8")
    env = clean_env(ws)
    db = ws / ".flywhl" / "harness.db"
    svc = FakeNotifyService(ws / ".flywhl" / "external_notify.db")

    run_id = crash(ws, env, svc)
    store = Store(db)
    title("崩溃现场")
    show_scene(store, run_id, svc)
    dead_worker = store.get_run(run_id).worker_id  # type: ignore[union-attr]

    title("进程 B：flywhl recover")
    time.sleep(STALE_AFTER + 0.5)          # 心跳过期之前，recover 不会抢一个可能还活着的 Worker
    out = subprocess.run(cli(ws, "recover"), cwd=ws, env=env, capture_output=True, text=True, timeout=120)
    print(D + out.stdout.strip() + X)
    if out.returncode:
        print(R + out.stderr[-2000:] + X)

    store = Store(db)
    run = store.get_run(run_id)
    assert run is not None
    steps = store.list_steps(run_id)
    moves = trail(store, run_id)
    title("状态迁移轨迹（每一次迁移都是一条 status 事件）")
    for i, (a, b, why, v) in enumerate(moves, 1):
        print(f"  {i:>2}. {a:>12} → {b:<12} v{v:<3} {D}{why}{X}")
    step = next((s for s in steps if s.tool == "send_notification"), None)
    if step is not None:
        print(f"\n  step {step.tool}：{step.status}  {D}{(step.output or {}).get('content', '')}{X}")

    checks = [
        ("Run 最终 completed", run.status == "completed"),
        ("外部通知恰好一次（对账而不是重发）", svc.count() == 1),
        ("中断的工具步骤以对账收尾（reconciled）", step is not None and step.status == "reconciled"),
        ("轨迹经过 running → interrupted → queued → running",
         [m[:2] for m in moves[2:5]] == [("running", "interrupted"), ("interrupted", "queued"), ("queued", "running")]),
        ("每一步迁移都在白名单内", all(b in TRANSITIONS[a] for a, b, *_ in moves)),
        ("版本号单调递增（CAS）", [m[3] for m in moves] == sorted({m[3] for m in moves})),
        ("接管者是另一个 Worker", run.worker_id is not None and run.worker_id != dead_worker),
    ]
    title("不变量")
    for name, ok in checks:
        print(f"  {G + '✓' if ok else R + '✗'} {name}{X}")
    print(f"\n  {B}Run {run_id}{X}：status = {run.status}，attempt = {run.attempt}，version = {run.version}，"
          f"外部通知数 = {svc.count()}")
    if keep:
        print(f"\n  工作区保留在 {ws}，可以继续查看：\n"
              f"    flywhl runs -w {ws} -c {ws / 'flywhl.toml'}\n    flywhl trace {run_id} -w {ws} -c {ws / 'flywhl.toml'}")
    else:
        shutil.rmtree(ws, ignore_errors=True)
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
