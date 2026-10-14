"""Run 生命周期的边界：失败重试、关闭时暂停、queued 孤儿、基于旧观察的接管、离线迁移的审计、验收期间的插话。"""

from __future__ import annotations

import asyncio
import os

from flywhl_harness import HookEvent, ScriptedProvider, Store
from flywhl_harness.events import E
from flywhl_harness.llm.base import ErrorKind, ProviderError
from flywhl_harness.tools.demo import FakeNotifyService

from .conftest import make_harness
from .test_recovery import SCRIPT


def status_events(h, run_id):
    return [(e.data["from"], e.data["to"]) for e in h.store.list_events(run_id) if e.type == E.STATUS]


async def test_retry_failed_run_continues_from_history(ws):
    """failed → queued（attempt + 1）：从持久化的历史继续，恢复说明里带上失败原因。"""
    h = make_harness(ws, ScriptedProvider([ProviderError(ErrorKind.AUTH, "401 密钥无效"), "重试成功"], mode="sequential"))
    s = h.create_session("t")
    run = await h.start_run(s.id, "hi")
    failed = await h.wait(run.id)
    assert failed.status == "failed"
    await h.retry(run.id)
    rec = await h.wait(run.id)
    assert rec.status == "completed" and rec.attempt == failed.attempt + 1
    notes = [m.text for m in h.log(s.id).messages() if m.kind == "resume_note"]
    assert notes and "失败" in notes[-1]
    assert ("failed", "queued") in status_events(h, run.id)


async def test_shutdown_pauses_then_restart_resumes(ws, tmp_path):
    """服务关闭：活动 Run 在安全点暂停（不是取消）；重启后 recover() 自动继续，副作用只发生一次。"""
    db = tmp_path / "h.db"
    h1 = make_harness(ws, SCRIPT, store=Store(db))
    h1.tools.get("send_notification").latency = 0.3
    s = h1.create_session("t")
    run = await h1.start_run(s.id, "发通知")
    await asyncio.sleep(0.1)
    await h1.aclose(grace=5)
    rec = h1.store.get_run(run.id)
    assert rec.status == "paused" and rec.checkpoint["paused_by"] == "shutdown"

    h2 = make_harness(ws, SCRIPT, store=Store(db))
    assert await h2.recover(auto_resume=True) == [run.id]
    assert (await h2.wait(run.id)).status == "completed"
    assert FakeNotifyService(h2.data_dir / "external_notify.db").count() == 1


async def test_user_pause_is_not_auto_resumed(ws, tmp_path):
    db = tmp_path / "h.db"
    h1 = make_harness(ws, SCRIPT, store=Store(db))
    h1.tools.get("send_notification").latency = 0.3
    s = h1.create_session("t")
    run = await h1.start_run(s.id, "发通知")
    await asyncio.sleep(0.1)
    await h1.pause(run.id)
    assert (await h1.wait(run.id)).status == "paused"
    h2 = make_harness(ws, SCRIPT, store=Store(db))
    assert await h2.recover(auto_resume=True) == [], "用户主动暂停的 Run 只能由用户恢复"


async def test_queued_orphan_is_recovered(ws, tmp_path):
    """创建后、开始执行前进程就退出：Run 停在 queued，接管时应当从原始 prompt 开始执行。"""
    db = tmp_path / "h.db"
    h1 = make_harness(ws, ["孤儿任务完成"], store=Store(db))
    s = h1.create_session("t")
    run = h1.store.create_run(s.id, "被遗忘的任务")
    h1._offline_transition(run.id, "queued", "已创建，等待执行")
    h2 = make_harness(ws, ["孤儿任务完成"], store=Store(db), stale_after_seconds=0.05)
    await asyncio.sleep(0.1)
    assert await h2.recover(auto_resume=True) == [run.id]
    rec = await h2.wait(run.id)
    assert rec.status == "completed" and rec.result["text"] == "孤儿任务完成"


async def test_takeover_based_on_stale_observation_is_fenced(ws, tmp_path, monkeypatch):
    """B 扫描时心跳已过期；但在 B 迁移之前，A 已经接管并推进了版本。B 的迁移必须失败，而不是“再接管一次”。"""
    db = tmp_path / "h.db"
    a = make_harness(ws, ["x"], store=Store(db), stale_after_seconds=0)
    s = a.create_session("t")
    run = a.store.create_run(s.id, "p")
    a.state.transition(run.id, "queued")
    a.state.transition(run.id, "running")
    seen = a.store.get_run(run.id)  # B 扫描时看到的版本
    for to in ("interrupted", "queued", "running"):  # A 抢先完成接管
        a.state.transition(run.id, to)
    b = make_harness(ws, ["x"], store=Store(db), stale_after_seconds=0)
    monkeypatch.setattr(b.store, "stale_runs", lambda *_a, **_k: [seen])
    assert await b.recover(auto_resume=False) == []
    assert b.store.get_run(run.id).status == "running"


def test_heartbeat_does_not_steal_lease(tmp_path):
    st = Store(tmp_path / "h.db")
    s = st.create_session("t", workspace=str(tmp_path))
    run = st.create_run(s.id, "p")
    st.update_run(run.id, worker_id="w-new")
    assert st.heartbeat(run.id, "w-new") is True
    assert st.heartbeat(run.id, "w-old") is False, "旧 Worker 的续租不能覆盖新主人的租约"
    assert st.get_run(run.id).worker_id == "w-new"


async def test_offline_transitions_are_audited(ws):
    h = make_harness(ws, SCRIPT)
    h.tools.get("send_notification").latency = 0.3
    s = h.create_session("t")
    run = await h.start_run(s.id, "发通知")
    await asyncio.sleep(0.05)
    await h.pause(run.id)
    await h.wait(run.id)
    await h.cancel(run.id, reason="不需要了")  # 离线取消：没有活动的 EventHub
    st = status_events(h, run.id)
    assert st[0] == ("created", "queued") and st[-1] == ("paused", "cancelled"), st
    seqs = [e.seq for e in h.store.list_events(run.id)]
    assert seqs == sorted(set(seqs)), "离线事件与在线事件的序号不能撞号"


async def test_follow_up_arriving_during_stop_check_is_processed(ws):
    """验收（Stop hook / 验收命令）期间到达的追加输入不能被丢弃：Loop 应当继续处理它。"""
    prov = ScriptedProvider(["第一版答复", "已处理追加的要求"], mode="sequential")
    h = make_harness(ws, prov)
    fired = []

    @h.hooks.on(HookEvent.STOP)
    async def late_input(inp):
        if not fired:
            fired.append(1)
            inp.context.inbox.follow_up("顺便把单位也写上")

    res = await h.run("算一下")
    assert res.status == "completed" and res.text == "已处理追加的要求"
    assert prov.calls == 2
    assert not any(e.type == E.NOTICE and e.data.get("code") == "inbox_dropped" for e in res.events)


async def test_write_is_atomic_and_keeps_mode(ws):
    target = ws / "run.sh"
    target.write_text("echo old\n")
    os.chmod(target, 0o755)
    h = make_harness(ws, [{"tool_calls": [{"name": "write", "args": {"path": "run.sh", "content": "echo new\n"}}]}, "好了"])
    res = await h.run("改脚本")
    assert res.status == "completed" and target.read_text() == "echo new\n"
    assert os.stat(target).st_mode & 0o777 == 0o755
    assert not list(ws.glob(".*.flywhl-tmp"))


async def test_fenced_worker_does_not_touch_the_new_owners_run(ws, tmp_path):
    """旧 Worker 假死期间 Run 被接管：它醒来后发现租约易主，必须像崩溃一样停下——不能把新主人的 Run 改成 cancelled。"""
    db = tmp_path / "h.db"
    a = make_harness(ws, SCRIPT, store=Store(db))
    a.tools.get("send_notification").latency = 2
    s = a.create_session("t")
    run = await a.start_run(s.id, "发通知")
    await asyncio.sleep(0.3)  # 通知请求已发出，正在等回包
    b = Store(db)              # 另一个 Worker 接管（模拟：心跳过期后的 interrupted → queued → running）
    for to, kw in (("interrupted", {}), ("queued", {}), ("running", {"worker_id": "w-new"})):
        a.state.__class__(b).transition(run.id, to, **kw)
    seq_at_takeover = b.max_event_seq(run.id)
    rec = await asyncio.wait_for(a.wait(run.id), 3)
    assert rec.status == "running" and rec.worker_id == "w-new", "旧 Worker 不能改写新主人的 Run"
    step = b.get_step(run.id, a.log(s.id).messages()[1].tool_calls[0].id)
    assert step.status == "started", "在途调用的结果留给新主人对账"
    assert b.max_event_seq(run.id) == seq_at_takeover, "租约丢失后不再写入事件"
