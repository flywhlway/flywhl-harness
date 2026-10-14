"""崩溃恢复：在工具执行中途杀掉进程，重启后最终结果一致、外部副作用只发生一次。"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from flywhl_harness import Store
from flywhl_harness.tools.demo import FakeNotifyService

from .conftest import make_harness

SCRIPT = [
    {"text": "先发通知", "tool_calls": [{"name": "send_notification", "args": {"to": "ops", "message": "部署完成"}}]},
    "通知已发送，任务完成。",
]


async def test_in_process_crash_then_recover(ws, tmp_path):
    db = tmp_path / "h.db"
    h1 = make_harness(ws, SCRIPT, store=Store(db))
    h1.tools.get("send_notification").latency = 5
    s = h1.create_session("t")
    run = await h1.start_run(s.id, "发通知")
    svc = FakeNotifyService(h1.data_dir / "external_notify.db")
    for _ in range(200):
        if svc.count() == 1:
            break
        await asyncio.sleep(0.02)
    # 模拟进程崩溃：强行取消驱动任务（不走协作式取消，状态停留在 running）
    h1._active[run.id].task.cancel()
    await asyncio.sleep(0.05)
    assert h1.store.get_run(run.id).status == "running"
    step = h1.store.get_step(run.id, h1.log(s.id).messages()[1].tool_calls[0].id)
    assert step.status == "started", "intent 已落盘、result 未落盘：结果未知"

    # 新 Worker 接管：心跳过期 → interrupted → 对账 → 继续
    h2 = make_harness(ws, SCRIPT, store=Store(db), stale_after_seconds=0.1)
    await asyncio.sleep(0.3)
    ids = await h2.recover(auto_resume=True)
    assert ids == [run.id]
    rec = await h2.wait(run.id)
    assert rec.status == "completed"
    assert svc.count() == 1, "外部副作用只能发生一次"
    tool_msg = [m for m in h2.log(s.id).messages() if m.role == "tool"][0]
    assert tool_msg.details["status"] == "reconciled"
    assert rec.attempt == 1 and rec.version > 3


async def test_recover_is_fenced(ws, tmp_path):
    """两个 Worker 同时接管同一个崩溃的 Run：CAS 保证只有一个成功。"""
    db = tmp_path / "h.db"
    h1 = make_harness(ws, ["x"], store=Store(db))
    s = h1.create_session("t")
    run = h1.store.create_run(s.id, "p")
    h1.state.transition(run.id, "queued")
    h1.state.transition(run.id, "running")
    a = make_harness(ws, ["x"], store=Store(db), stale_after_seconds=0)
    b = make_harness(ws, ["x"], store=Store(db), stale_after_seconds=0)
    ra = await a.recover(auto_resume=False)
    rb = await b.recover(auto_resume=False)
    assert sorted(ra + rb) == [run.id]


CHILD = textwrap.dedent('''
    import asyncio, sys
    sys.path.insert(0, sys.argv[3])
    from flywhl_harness import Store
    from tests.conftest import make_harness
    from tests.test_recovery import SCRIPT
    from pathlib import Path

    async def main():
        h = make_harness(Path(sys.argv[1]), SCRIPT, store=Store(sys.argv[2]))
        h.tools.get("send_notification").latency = 30
        s = h.create_session("crash")
        run = await h.start_run(s.id, "发通知")
        print(run.id, flush=True)
        await h.wait(run.id)
    asyncio.run(main())
''')


async def test_kill_dash_9_then_recover(ws, tmp_path):
    """真正的 kill -9：进程没有任何机会清理。"""
    db = tmp_path / "h.db"
    repo = str(Path(__file__).resolve().parents[1])
    proc = subprocess.Popen([sys.executable, "-c", CHILD, str(ws), str(db), repo], stdout=subprocess.PIPE, text=True, cwd=repo)
    run_id = proc.stdout.readline().strip()
    svc = FakeNotifyService(ws / ".flywhl" / "external_notify.db")
    deadline = time.time() + 15
    while svc.count() == 0 and time.time() < deadline:
        time.sleep(0.05)
    assert svc.count() == 1
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()

    h = make_harness(ws, SCRIPT, store=Store(db), stale_after_seconds=0.1)
    await asyncio.sleep(0.3)
    assert await h.recover() == [run_id]
    rec = await h.wait(run_id)
    assert rec.status == "completed" and svc.count() == 1


async def test_recovered_result_precedes_resume_note(ws, tmp_path):
    """回归：恢复时先补齐 tool 结果、再追加恢复说明；发给模型的历史里真实的对账结果不能被当作孤立结果丢弃。"""
    from flywhl_harness.llm.base import prepare_history

    db = tmp_path / "h.db"
    h1 = make_harness(ws, SCRIPT, store=Store(db))
    h1.tools.get("send_notification").latency = 5
    s = h1.create_session("t")
    run = await h1.start_run(s.id, "发通知")
    svc = FakeNotifyService(h1.data_dir / "external_notify.db")
    for _ in range(200):
        if svc.count() == 1:
            break
        await asyncio.sleep(0.02)
    h1._active[run.id].task.cancel()
    await asyncio.sleep(0.05)
    h2 = make_harness(ws, SCRIPT, store=Store(db), stale_after_seconds=0.1)
    await asyncio.sleep(0.3)
    await h2.recover()
    await h2.wait(run.id)
    msgs = h2.log(s.id).messages()
    roles = [(m.role, m.kind) for m in msgs]
    i_tool = next(i for i, (r, _) in enumerate(roles) if r == "tool")
    i_note = next(i for i, (_, k) in enumerate(roles) if k == "resume_note")
    assert i_tool < i_note, roles
    sent = prepare_history(msgs)
    tool_msgs = [m for m in sent if m.role == "tool"]
    assert len(tool_msgs) == 1 and "对账" in tool_msgs[0].text
