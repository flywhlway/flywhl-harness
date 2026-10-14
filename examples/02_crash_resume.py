"""02 · 崩溃恢复：在副作用工具执行中途 kill -9，重启后结果一致、副作用只发生一次。

运行：python examples/02_crash_resume.py

时间线：
  子进程启动 Run → 模型调用 send_notification → 预写 intent（started）→ 外部系统已记录通知
  → 等待回包期间被 kill -9（没有任何清理机会）
  → 新进程接管：心跳过期 → CAS 迁移为 interrupted → 恢复：发现“有 intent 无 result”的调用
  → 按幂等键向外部系统对账 → 确认已发送 → 补写结果，不重发 → 继续下一轮 → completed
"""

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from _common import G, R, X, harness, say, temp_workspace, title

from flywhl_harness import ScriptedProvider, Store
from flywhl_harness.tools.demo import FakeNotifyService

SCRIPT = [
    {"text": "部署已完成，通知运维。", "tool_calls": [{"name": "send_notification", "args": {"to": "ops-team", "message": "v1.2.0 已上线"}}]},
    "通知已发送，任务完成。",
]


async def worker(ws: str, db: str) -> None:
    """子进程：启动 Run，然后在工具执行中途被父进程 kill -9。"""
    h = harness(Path(ws), ScriptedProvider(SCRIPT), store=Store(db))
    h.tools.get("send_notification").latency = 60  # 回包很慢：给 kill -9 留出窗口
    s = h.create_session("崩溃演示")
    run = await h.start_run(s.id, "发布后通知运维")
    print(run.id, flush=True)
    await h.wait(run.id)


async def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        return await worker(sys.argv[2], sys.argv[3])
    title("02 崩溃恢复（kill -9）")
    ws = temp_workspace("02")
    db = ws / ".flywhl" / "harness.db"
    proc = subprocess.Popen([sys.executable, __file__, "--worker", str(ws), str(db)], stdout=subprocess.PIPE, text=True)
    run_id = proc.stdout.readline().strip()
    say(f"子进程 {proc.pid} 启动了 Run {run_id}")
    svc = FakeNotifyService(ws / ".flywhl" / "external_notify.db")
    while svc.count() == 0:
        time.sleep(0.05)
    say("外部系统已经收到通知，但子进程还没拿到回包 → 现在 kill -9")
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()
    st = Store(db)
    print(f"  崩溃现场：run.status = {st.get_run(run_id).status}，steps = "
          f"{[(s.tool, s.status) for s in st.list_steps(run_id)]}，外部通知数 = {svc.count()}")

    title("新进程接管")
    h = harness(ws, ScriptedProvider(SCRIPT), store=Store(db), stale_after_seconds=0.5)
    await asyncio.sleep(0.6)
    ids = await h.recover(auto_resume=True)
    say(f"recover() 接管了：{ids}")
    rec = await h.wait(run_id)
    tool_msg = [m for m in h.log(rec.session_id).messages() if m.role == "tool"][0]
    print(f"  工具结果状态：{tool_msg.details['status']}  内容：{tool_msg.text}")
    print(f"  Run 最终状态：{rec.status}（version={rec.version}，attempt={rec.attempt}）")
    ok = rec.status == "completed" and svc.count() == 1
    print(f"\n  {G if ok else R}外部通知总数 = {svc.count()} {'✓ 副作用恰好一次' if ok else '✗'}{X}")
    await h.aclose()


if __name__ == "__main__":
    asyncio.run(main())
