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

import pytest

from flywhl_harness import HookEvent, Store
from flywhl_harness.runtime.store import ApprovalRecord
from flywhl_harness.tools.base import Effect, tool
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


# ---------------------------------------------------------------- 接管的边界
SLOW = [{"tool_calls": [{"name": "sleep", "args": {"seconds": 5}}]}, "完成", "完成", "完成"]


def _running(h, session_id: str, *, parent: str | None = None, worker: str = "w-dead") -> str:
    """造一个停在 running 的 Run：Worker 已经不在。"""
    run = h.store.create_run(session_id, "p", parent_run_id=parent)
    h.state.transition(run.id, "queued")
    h.state.transition(run.id, "running", worker_id=worker)
    return run.id


async def _until(cond, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "等待超时"
        await asyncio.sleep(0.01)


async def test_a_failed_resume_does_not_stop_the_rest_of_the_takeover(ws):
    h = make_harness(ws, list(SLOW), stale_after_seconds=0)
    busy = h.create_session("甲")
    holder = await h.start_run(busy.id, "占住会话")
    blocked = _running(h, busy.id)
    other = _running(h, h.create_session("乙").id)

    recovered = await h.recover()

    assert other in recovered and blocked not in recovered
    assert h.store.get_run(blocked).status == "interrupted", "恢复不起来的 Run 留给人手动恢复"
    for rid in (holder.id, other):
        await h.cancel(rid)
        await h.wait(rid)


async def test_a_paused_run_that_cannot_resume_is_not_reported_as_recovered(ws):
    h = make_harness(ws, list(SLOW), stale_after_seconds=0)
    s = h.create_session("甲")
    blocked = _running(h, s.id)
    h.state.transition(blocked, "paused")
    h.store.update_run(blocked, checkpoint={"paused_by": "shutdown"})
    holder = await h.start_run(s.id, "占住会话")

    assert blocked not in await h.recover()

    await h.cancel(holder.id)
    await h.wait(holder.id)


async def test_a_queued_orphan_waits_until_its_session_is_free(ws):
    h = make_harness(ws, list(SLOW), stale_after_seconds=0)
    s = h.create_session("甲")
    holder = await h.start_run(s.id, "占住会话")
    orphan = h.store.create_run(s.id, "孤儿")
    h.state.transition(orphan.id, "queued")
    await asyncio.sleep(0.01)

    assert orphan.id not in await h.recover()
    assert h.store.get_run(orphan.id).status == "queued" and orphan.id not in h._active

    await h.cancel(holder.id)
    await h.wait(holder.id)
    assert orphan.id in await h.recover()
    await h.wait(orphan.id)


@pytest.mark.parametrize("kind", ["cancelling", "child"])
async def test_takeover_expires_approvals_of_runs_it_will_not_resume(ws, kind):
    h = make_harness(ws, ["不应被调用"], stale_after_seconds=0)
    s = h.create_session("甲")
    parent = _running(h, s.id) if kind == "child" else None
    run_id = _running(h, s.id, parent=parent)
    if kind == "cancelling":
        h.state.transition(run_id, "cancelling")
    h.store.create_approval(ApprovalRecord("ap", run_id, s.id, "call_1", "write", {"path": "a.txt"}, "写文件", "medium", "要确认", "pending"))

    await h.recover(auto_resume=False)

    [ap] = h.store.list_approvals(None, run_id)
    assert ap.status == "expired" and ap.decided_by == "system:recover"


# ---------------------------------------------------------------- 收尾
def _holding_tool():
    gate = asyncio.Event()
    entered: list[bool] = []

    @tool(name="hold", effect=Effect.READ)
    async def hold(ctx) -> str:
        """等用例放行：这段时间里 Worker 的心跳不会醒。"""
        entered.append(True)
        await gate.wait()
        return "完成"

    return hold, gate, entered


@pytest.mark.parametrize(("path", "final"), [(["interrupted"], "interrupted"),
                                             (["interrupted", "queued", "running"], "running")])
async def test_a_worker_that_lost_the_run_does_not_finish_it(ws, path, final):
    hold, gate, entered = _holding_tool()
    h = make_harness(ws, [{"tool_calls": [{"name": "hold", "args": {}}]}, "旧 Worker 的结论"], heartbeat_seconds=60)
    h.tools.register(hold)
    s = h.create_session("甲")
    run = await h.start_run(s.id, "x")
    await _until(lambda: entered)

    for to in path:     # 别的 Worker 接管：心跳下一次醒来才会发现
        h.state.transition(run.id, to, worker_id="w-new")
    gate.set()
    await _until(lambda: run.id not in h._active)

    rec = h.store.get_run(run.id)
    assert (rec.status, rec.worker_id, rec.result) == (final, "w-new", None)


async def test_cancel_beating_completion_is_recorded_as_cancel(ws):
    h = make_harness(ws, ["答复"])

    @h.hooks.on(HookEvent.STOP)
    async def late_cancel(inp):
        assert await h.cancel(inp.run_id, reason="来不及了", by="alice")

    res = await h.run("甲")
    rec = h.store.get_run(res.run_id)
    assert rec.status == "cancelled" and "发起人：alice" in rec.status_reason
    assert "取消请求先于完成到达" in rec.result["reason"] and rec.result["by"] == "alice"


async def test_run_and_aclose_wait_for_the_run_end_hook(ws):
    h = make_harness(ws, ["完成", "完成"])
    seen: list[str] = []

    @h.hooks.on(HookEvent.RUN_END)
    async def notify(inp):
        await asyncio.sleep(0.05)
        seen.append(inp.run_id)

    res = await h.run("甲")
    assert seen == [res.run_id], "CLI 跑完就退出：run() 返回时 hook 不能还在半路"

    s = h.create_session("乙")
    run = await h.start_run(s.id, "乙")
    await _until(lambda: run.id not in h._active)
    await h.aclose()
    assert seen == [res.run_id, run.id]
