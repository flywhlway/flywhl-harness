"""运行时管理器在边界条件下的行为。

这批用例测这几类场景：事件扇出与断线续传（订阅者掉队、Run 结束时队列正满、重复投递）；控制面对不存在、已结束、
正忙的 Run 的拒绝；崩溃接管（子 Run、取消中的 Run、待审批、queued 孤儿）；悬空工具调用的离线配对；
租约丢失后的围栏；子代理失败、被抢占、读过不可信内容；以及 hook 与关闭流程。
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import pytest

from flywhl_harness import ScriptedProvider, Store
from flywhl_harness.events import E, Event
from flywhl_harness.policy.hooks import HookEvent, HookOutput
from flywhl_harness.runtime.manager import _CLOSED, _LAGGED, ActiveRun, EventHub
from flywhl_harness.runtime.state import IllegalTransition
from flywhl_harness.tools.base import Effect, Tool, ToolResult
from flywhl_harness.types import Message, ToolCall

from .conftest import make_harness, tool_ends
from .test_runtime import RoleRouter


def _hub_on_run(h, s, queue_size: int) -> tuple[str, EventHub]:
    """给一个真实 Run 挂上真实 EventHub，不起 Loop：事件的节奏由用例控制。"""
    run = h.store.create_run(s.id, "p")
    hub = EventHub(h.store, run.id, queue_size=queue_size)
    h._active[run.id] = ActiveRun(run.id, s.id, None, None, hub)  # type: ignore[arg-type]
    return run.id, hub


def _notice(text: str) -> Event:
    return Event(E.NOTICE, {"text": text})


def _codes(events) -> list[str]:
    return [e.data.get("code") for e in events if e.type == E.NOTICE]


async def _wait_until(cond, timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < deadline, "等待超时"
        await asyncio.sleep(0.01)


def _running_run(h, session_id: str, *, parent: str | None = None, worker: str = "w-dead") -> str:
    """造一个停在 running 的 Run：Worker 已经不在，心跳字段是空的。"""
    run = h.store.create_run(session_id, "p", parent_run_id=parent)
    h.state.transition(run.id, "queued")
    h.state.transition(run.id, "running", worker_id=worker)
    return run.id


def _dangling_call(h, session_id: str, run_id: str, *, started: bool = False) -> str:
    """在会话里留一个没有结果的工具调用；started 表示副作用已经发出，结果未知。"""
    call = ToolCall("call_1", "send_notification", {"to": "ops", "message": "x"})
    h.log(session_id).append_message(Message(role="assistant", content=[call]), run_id)
    if started:
        h.store.step_started(run_id, call.id, tool=call.name, effect="external", idem_key="idem-1", input_hash="h")
    return call.id


def _tool_msgs(h, session_id: str) -> list[Message]:
    return [m for m in h.log(session_id).messages() if m.role == "tool"]


def _status_path(h, run_id: str) -> list[tuple[str, str]]:
    return [(e.data["from"], e.data["to"]) for e in h.store.list_events(run_id) if e.type == E.STATUS]


def _child_running(h, run_id: str):
    """子 Run 已经进入工具调用（它的 assistant 消息落库之后）才返回。"""
    for r in h.store.child_runs(run_id):
        if r.status == "running" and any(m.role == "assistant" for m in h.log(r.session_id).messages()):
            return r
    return None


SLOW = {"tool_calls": [{"name": "sleep", "args": {"seconds": 5}}]}
DELEGATE = {"tool_calls": [{"name": "task", "args": {"description": "慢查", "prompt": "等一会儿"}}]}


# ---------------------------------------------------------------------- 事件扇出
async def test_slow_subscriber_is_dropped_while_healthy_one_keeps_receiving(ws):
    """慢订阅者队列满了只踢它自己：积压换成掉队信号，健康订阅者和落库都不受影响。"""
    h = make_harness(ws, [])
    run_id, hub = _hub_on_run(h, h.create_session("t"), queue_size=2)
    slow = hub.subscribe()
    hub.queue_size = 100
    fast = hub.subscribe()
    for i in range(3):
        hub.publish(_notice(str(i)))
    assert slow.get_nowait() is _LAGGED and slow.empty(), "积压要清空，只留掉队信号"
    assert [fast.get_nowait().data["text"] for _ in range(3)] == ["0", "1", "2"]
    hub.publish(_notice("3"))
    assert slow.empty(), "踢下线之后不再投递"
    assert fast.get_nowait().data["text"] == "3"
    assert [e.data["text"] for e in h.store.list_events(run_id)] == ["0", "1", "2", "3"], "掉队者靠库补齐，落库不能少"


async def test_close_with_full_queue_still_delivers_closed_signal(ws):
    h = make_harness(ws, [])
    _, hub = _hub_on_run(h, h.create_session("t"), queue_size=1)
    q = hub.subscribe()
    hub.publish(_notice("积压"))
    assert q.full()
    hub.close()
    assert q.get_nowait() is _CLOSED and q.empty()
    assert hub.closed and hub._subs == []


# ---------------------------------------------------------------------- 断线续传
async def test_subscriber_behind_at_close_gets_tail_from_store(ws):
    """Run 结束时订阅者队列正满：积压被关闭信号替换，尾巴要从库里补齐。"""
    h = make_harness(ws, [])
    run_id, hub = _hub_on_run(h, h.create_session("t"), queue_size=2)
    hub.publish(_notice("1"))
    gen = h.subscribe(run_id)
    first = await gen.__anext__()
    hub.publish(_notice("2"))
    hub.publish(_notice("3"))
    hub.close()
    h._active.pop(run_id)
    rest = [ev async for ev in gen]
    assert [e.data["text"] for e in [first, *rest]] == ["1", "2", "3"]


async def test_event_buffered_before_subscribe_is_delivered_once(ws):
    """订阅时 flush 会把缓冲的增量同时送进队列和库：补齐读到一次，队列里那份要按 seq 去重。"""
    h = make_harness(ws, [])
    run_id, hub = _hub_on_run(h, h.create_session("t"), queue_size=10)
    hub.publish(Event(E.MESSAGE_UPDATE, {"delta": "缓冲中"}))
    gen = h.subscribe(run_id)
    seen = [await gen.__anext__()]
    hub.publish(_notice("实时"))
    seen.append(await gen.__anext__())
    hub.close()
    seen += [ev async for ev in gen]
    assert [e.seq for e in seen] == [1, 2], "同一个 seq 不能投递两次"
    assert [e.type for e in seen] == [E.MESSAGE_UPDATE, E.NOTICE]


async def test_lagged_subscriber_catches_up_and_rejoins_live_stream(ws):
    """掉队后 Run 还在跑：重新订阅、从库里补齐，再接上实时流，全程不重不漏。"""
    h = make_harness(ws, [])
    run_id, hub = _hub_on_run(h, h.create_session("t"), queue_size=2)
    hub.publish(_notice("0"))
    gen = h.subscribe(run_id)
    seen = [await gen.__anext__()]
    for i in range(1, 4):  # 第三条挤爆容量为 2 的队列
        hub.publish(_notice(str(i)))
    assert hub._subs == [], "已被踢下线"
    seen += [await gen.__anext__() for _ in range(3)]
    assert len(hub._subs) == 1, "补齐之前要先重新订阅"
    hub.publish(_notice("4"))
    seen.append(await gen.__anext__())
    hub.close()
    seen += [ev async for ev in gen]
    assert [e.data["text"] for e in seen] == ["0", "1", "2", "3", "4"]


async def test_lagged_subscriber_finishes_from_store_when_run_is_gone(ws):
    """掉队信号被读到时 Run 已经结束：没有新队列可接，从库里补完就结束，不能挂死。"""
    h = make_harness(ws, [])
    run_id, hub = _hub_on_run(h, h.create_session("t"), queue_size=1)
    hub.publish(_notice("0"))
    gen = h.subscribe(run_id)
    seen = [await gen.__anext__()]
    hub.publish(_notice("1"))
    hub.publish(_notice("2"))  # 挤爆
    hub.close()
    h._active.pop(run_id)
    seen += [ev async for ev in gen]
    assert [e.data["text"] for e in seen] == ["0", "1", "2"]


async def test_abandoned_subscriber_detaches_from_hub(ws):
    """客户端中途断开：队列要从枢纽摘掉，否则之后的事件会一直堆在没人读的队列里。"""
    h = make_harness(ws, [])
    run_id, hub = _hub_on_run(h, h.create_session("t"), queue_size=10)
    hub.publish(_notice("0"))
    gen = h.subscribe(run_id)
    await gen.__anext__()
    assert len(hub._subs) == 1
    await gen.aclose()
    assert hub._subs == []


async def test_run_on_event_accepts_sync_and_async_callbacks(ws):
    h = make_harness(ws, ["一", "二"])
    sync_seen: list[int] = []
    async_seen: list[int] = []

    async def collect(ev):
        async_seen.append(ev.seq)

    first = await h.run("甲", on_event=lambda ev: sync_seen.append(ev.seq))
    second = await h.run("乙", session_id=first.session_id, on_event=collect)
    assert sync_seen == [e.seq for e in first.events] and sync_seen
    assert async_seen == [e.seq for e in second.events] and async_seen


# ---------------------------------------------------------------------- 控制面的拒绝路径
async def test_controls_refuse_finished_or_unknown_runs(ws):
    h = make_harness(ws, ["完成"])
    res = await h.run("甲")
    assert await h.pause(res.run_id) is False
    assert h.steer(res.run_id, "晚了") is False
    assert h.follow_up(res.run_id, "晚了") is False
    with pytest.raises(RuntimeError, match="只有 failed"):
        await h.retry(res.run_id)
    with pytest.raises(RuntimeError, match="只有 failed"):
        await h.retry("不存在")
    with pytest.raises(KeyError):
        await h.resume("不存在")
    with pytest.raises(KeyError, match="会话不存在"):
        await h.start_run("不存在", "甲")


async def test_busy_session_refuses_conflicting_operations(ws):
    h = make_harness(ws, [{"tool_calls": [{"name": "sleep", "args": {"seconds": 5}}]}, "完成"])
    s = h.create_session("t")
    run = await h.start_run(s.id, "慢任务")
    with pytest.raises(RuntimeError, match="已有运行中的任务"):
        await h.start_run(s.id, "再来一个")
    with pytest.raises(RuntimeError, match="正在执行中"):
        await h.resume(run.id)
    with pytest.raises(RuntimeError, match="运行结束后再手动压缩"):
        await h.compact(s.id)
    with pytest.raises(RuntimeError, match="无法切换分支"):
        h.branch(s.id, None)
    await h.cancel(run.id)
    assert (await h.wait(run.id)).status == "cancelled"


async def test_resume_refused_while_another_run_holds_the_session(ws):
    script = ScriptedProvider([{"tool_calls": [{"name": "sleep", "args": {"seconds": 0.05}}]},
                               {"tool_calls": [{"name": "sleep", "args": {"seconds": 5}}]}], mode="sequential")
    h = make_harness(ws, script)
    s = h.create_session("t")
    first = await h.start_run(s.id, "先暂停")
    await h.pause(first.id)
    assert (await h.wait(first.id)).status == "paused"
    second = await h.start_run(s.id, "占住会话")
    with pytest.raises(RuntimeError, match=f"已有运行中的任务 {second.id}"):
        await h.resume(first.id)
    assert h.store.get_run(first.id).status == "paused", "拒绝恢复不能改动状态"
    await h.cancel(second.id)
    await h.wait(second.id)


async def test_set_mode_persists_valid_mode_and_rejects_unknown(ws):
    h = make_harness(ws, [])
    s = h.create_session("t")
    h.set_mode(s.id, "plan")
    assert h.store.get_session(s.id).meta["mode"] == "plan"
    with pytest.raises(ValueError):
        h.set_mode(s.id, "不存在的模式")
    assert h.store.get_session(s.id).meta["mode"] == "plan", "校验失败不能落库"


async def test_model_switch_is_recorded_once(ws):
    """老会话换模型继续：记一条 notice 并更新会话，下一个 Run 不再重复提示。"""
    h = make_harness(ws, ["一", "二"])
    s = h.create_session("t")
    h.store.update_session(s.id, meta={**s.meta, "model": "旧模型"})
    first = await h.run("甲", session_id=s.id)
    switched = [e for e in first.events if e.data.get("code") == "model_switched"]
    assert len(switched) == 1 and "旧模型" in switched[0].data["text"] and "scripted" in switched[0].data["text"]
    assert h.store.get_session(s.id).meta["model"] == "scripted"
    second = await h.run("乙", session_id=s.id)
    assert "model_switched" not in _codes(second.events)


async def test_resume_carries_steer_queued_at_pause_into_the_next_turn(ws):
    """暂停时还没注入的插话要随检查点带回：恢复后模型能看到，不会丢。"""
    prov = ScriptedProvider([{"tool_calls": [{"name": "sleep", "args": {"seconds": 0.1}}]}, "按方案 B 收尾"], mode="sequential")
    h = make_harness(ws, prov)
    s = h.create_session("t")
    run = await h.start_run(s.id, "甲")
    await _wait_until(lambda: any(m.role == "assistant" for m in h.log(s.id).messages()))
    await h.pause(run.id)
    assert h.steer(run.id, "改用方案 B")
    assert (await h.wait(run.id)).status == "paused"
    assert h.store.get_run(run.id).checkpoint["inbox"]["steer"] == ["改用方案 B"]
    await h.resume(run.id)
    assert (await h.wait(run.id)).status == "completed"
    assert any(m.role == "user" and "改用方案 B" in m.text for m in prov.requests[-1].messages)


# ---------------------------------------------------------------------- 取消
async def test_cancel_before_run_starts_stops_without_calling_model(ws):
    prov = ScriptedProvider(["不应被调用"])
    h = make_harness(ws, prov)
    s = h.create_session("t")
    run = await h.start_run(s.id, "甲")
    assert run.status == "queued"
    assert await h.cancel(run.id) is True
    assert (await h.wait(run.id)).status == "cancelled"
    assert prov.calls == 0


async def test_cancel_refused_for_run_owned_by_another_worker(ws):
    """Run 在别的 Worker 上跑：本进程没有它的令牌，不能离线改写状态。"""
    h = make_harness(ws, [])
    s = h.create_session("t")
    run_id = _running_run(h, s.id, worker="w-other")
    assert await h.cancel(run_id) is False
    assert h.store.get_run(run_id).status == "running"


async def test_cancel_losing_cas_to_completion_changes_nothing(ws, monkeypatch):
    """取消与完成竞争：CAS 输给完成者时返回 False，也不能再发取消令牌。"""
    h = make_harness(ws, [SLOW, "完成"])
    s = h.create_session("t")
    run = await h.start_run(s.id, "甲")
    await _wait_until(lambda: h.store.get_run(run.id).status == "running")

    def lost(*_a, **_k):
        raise IllegalTransition("已经完成")

    monkeypatch.setattr(h.state, "transition", lost)
    assert await h.cancel(run.id) is False
    assert not h._active[run.id].rc.cancel.cancelled
    monkeypatch.undo()
    await h.cancel(run.id)
    await h.wait(run.id)


async def test_cancel_arriving_during_final_stop_check_wins(ws):
    """Loop 已经给出完成结论，取消先一步占了 cancelling：最终状态以取消为准。"""
    h = make_harness(ws, ["答复"])

    @h.hooks.on(HookEvent.STOP)
    async def late_cancel(inp):
        assert await h.cancel(inp.run_id, reason="来不及了")

    res = await h.run("甲")
    rec = h.store.get_run(res.run_id)
    assert rec.status == "cancelled"
    assert "取消请求先于完成到达" in rec.status_reason
    assert _status_path(h, res.run_id)[-2:] == [("running", "cancelling"), ("cancelling", "cancelled")]


async def test_failure_while_waiting_for_approval_ends_run_as_failed(ws, monkeypatch):
    """审批等待中存储出错：Run 停在 waiting_human 上的 Loop 已结束，要先回 running 才能落到 failed。"""
    h = make_harness(ws, [{"tool_calls": [{"name": "write", "args": {"path": "a.txt", "content": "x"}}]}, "好"], on_ask="wait")
    h.approvals.timeout = 0.05

    def broken(*_a, **_k):
        raise RuntimeError("磁盘已满")

    monkeypatch.setattr(h.store, "resolve_approval", broken)
    s = h.create_session("t")
    run = await h.start_run(s.id, "写文件")
    rec = await h.wait(run.id)
    assert rec.status == "failed" and "磁盘已满" in rec.status_reason
    assert _status_path(h, run.id)[-3:] == [("running", "waiting_human"), ("waiting_human", "running"), ("running", "failed")]
    assert not (ws / "a.txt").exists()


# ---------------------------------------------------------------------- 崩溃接管
async def test_recover_leaves_runs_of_this_process_alone(ws):
    """心跳看起来过期，但 Run 就在本进程里跑：不能把自己接管了。"""
    h = make_harness(ws, [SLOW, "完成"], stale_after_seconds=0)
    s = h.create_session("t")
    run = await h.start_run(s.id, "甲")
    await _wait_until(lambda: h.store.get_run(run.id).status == "running")
    assert await h.recover() == []
    assert h.store.get_run(run.id).status == "running"
    await h.cancel(run.id)
    await h.wait(run.id)


@pytest.mark.parametrize("status, path", [
    ("running", [("running", "interrupted"), ("interrupted", "cancelled")]),
    ("cancelling", [("cancelling", "cancelled")]),
])
async def test_recover_closes_orphan_child_instead_of_resuming_it(ws, status, path):
    """子 Run 不单独恢复：只收尾并补齐悬空调用，父 Run 恢复时会重新派生。"""
    h = make_harness(ws, ["不应被调用"], stale_after_seconds=0)
    parent_s = h.create_session("父")
    parent = _running_run(h, parent_s.id)
    child_s = h.create_session("子", parent_session_id=parent_s.id)
    child = _running_run(h, child_s.id, parent=parent)
    if status == "cancelling":
        h.state.transition(child, "cancelling")
    call_id = _dangling_call(h, child_s.id, child)

    assert await h.recover(auto_resume=False) == [parent], "只有父 Run 算被接管"
    assert h.store.get_run(child).status == "cancelled"
    assert _status_path(h, child) == path
    [tool] = _tool_msgs(h, child_s.id)
    assert tool.tool_call_id == call_id and tool.details["status"] == "cancelled" and "重新派生" in tool.text


async def test_recover_finishes_cancellation_interrupted_by_crash(ws):
    """取消进行到一半进程崩溃：接管时把取消做完，不再恢复；已发出的调用记成结果未知。"""
    prov = ScriptedProvider(["不应被调用"])
    h = make_harness(ws, prov, stale_after_seconds=0)
    s = h.create_session("t")
    run_id = _running_run(h, s.id)
    h.state.transition(run_id, "cancelling")
    call_id = _dangling_call(h, s.id, run_id, started=True)

    assert await h.recover(auto_resume=True) == []
    assert h.store.get_run(run_id).status == "cancelled" and prov.calls == 0
    [tool] = _tool_msgs(h, s.id)
    assert tool.details["status"] == "unknown" and "idem-1" in tool.text
    assert h.store.get_step(run_id, call_id).status == "unknown"


async def test_takeover_expires_approval_left_by_crashed_worker(ws, tmp_path):
    db = tmp_path / "h.db"
    script = [{"tool_calls": [{"name": "write", "args": {"path": "a.txt", "content": "x"}}]}, "好"]
    h1 = make_harness(ws, script, store=Store(db), on_ask="wait")
    s = h1.create_session("t")
    run = await h1.start_run(s.id, "写文件")
    await _wait_until(lambda: h1.store.get_run(run.id).status == "waiting_human")
    h1._active[run.id].task.cancel()
    await h1.wait(run.id)
    assert [a.status for a in h1.store.list_approvals(None, run.id)] == ["pending"]

    h2 = make_harness(ws, script, store=Store(db), stale_after_seconds=0)
    assert await h2.recover(auto_resume=False) == [run.id]
    assert h2.store.get_run(run.id).status == "interrupted"
    [ap] = h2.store.list_approvals(None, run.id)
    assert ap.status == "expired" and ap.decided_by == "system:recover"


async def test_recover_without_auto_resume_only_reports_orphans(ws):
    h = make_harness(ws, [], stale_after_seconds=0)
    queued = h.store.create_run(h.create_session("甲").id, "孤儿").id
    h.state.transition(queued, "queued")
    paused = _running_run(h, h.create_session("乙").id)
    h.state.transition(paused, "paused")
    h.store.update_run(paused, checkpoint={"paused_by": "shutdown"})
    await asyncio.sleep(0.01)
    assert sorted(await h.recover(auto_resume=False)) == sorted([queued, paused])
    assert h._active == {}, "只上报，不启动"
    assert (h.store.get_run(queued).status, h.store.get_run(paused).status) == ("queued", "paused")


async def test_recover_skips_paused_run_blocked_by_busy_session(ws):
    """关闭时暂停的 Run 恢复不了（会话被新 Run 占着）：留在 paused，别的会话照常恢复。"""
    prov = ScriptedProvider([{"tool_calls": [{"name": "sleep", "args": {"seconds": 5}}]}, "完成"])
    h = make_harness(ws, prov)
    busy_s = h.create_session("甲")
    blocked = _running_run(h, busy_s.id)
    h.state.transition(blocked, "paused")
    h.store.update_run(blocked, checkpoint={"paused_by": "shutdown"})
    holder = await h.start_run(busy_s.id, "占住会话")

    other = _running_run(h, h.create_session("乙").id)
    h.state.transition(other, "paused")
    h.store.update_run(other, checkpoint={"paused_by": "shutdown"})

    other_s = h.store.get_run(other).session_id
    assert other in await h.recover()
    assert h.store.get_run(blocked).status == "paused"
    assert h.active_run_of(busy_s.id) == holder.id
    await _wait_until(lambda: h.active_run_of(other_s) == other and any(m.role == "assistant" for m in h.log(other_s).messages()))
    await _wait_until(lambda: any(m.role == "assistant" for m in h.log(busy_s.id).messages()))
    for rid in (holder.id, other):
        await h.cancel(rid)
        await h.wait(rid)


async def test_two_workers_starting_the_same_orphan_run_it_once(ws, tmp_path):
    """两个 Worker 都看到 queued 孤儿并启动：认领是 CAS，输的一方安静退出，绝不能把赢家的 Run 标成 failed。"""
    db = tmp_path / "h.db"
    seed = make_harness(ws, [], store=Store(db))
    s = seed.create_session("t")
    run = seed.store.create_run(s.id, "孤儿")
    seed._offline_transition(run.id, "queued", "等待执行")
    prov_a, prov_b = ScriptedProvider(["甲完成"]), ScriptedProvider(["乙完成"])
    a = make_harness(ws, prov_a, store=Store(db), stale_after_seconds=0.01)
    b = make_harness(ws, prov_b, store=Store(db), stale_after_seconds=0.01)
    await asyncio.sleep(0.05)
    assert await a.recover() == [run.id]
    assert await b.recover() == [run.id], "两边都还没开始跑：都认为要接手"
    await asyncio.wait([a._active[run.id].task, b._active[run.id].task])
    rec = a.store.get_run(run.id)
    assert rec.status == "completed" and rec.result["text"] == "甲完成"
    assert (prov_a.calls, prov_b.calls) == (1, 0), "模型只被调用一次"
    assert a._active == {} and b._active == {}
    assert "failed" not in [to for _, to in _status_path(a, run.id)]


async def test_recovery_loop_keeps_sweeping_after_a_failed_round(ws, monkeypatch):
    """周期接管的一轮失败不能让循环退出：崩溃的 Worker 要等后面几轮才会过期。"""
    h = make_harness(ws, ["接管后完成"], stale_after_seconds=0.01)
    s = h.create_session("t")
    run = h.store.create_run(s.id, "孤儿")
    h.state.transition(run.id, "queued")
    real, rounds = h.recover, []

    async def flaky(auto_resume=True):
        rounds.append(auto_resume)
        if len(rounds) == 1:
            raise RuntimeError("这一轮失败")
        return await real(auto_resume=auto_resume)

    monkeypatch.setattr(h, "recover", flaky)
    loop = asyncio.create_task(h.recovery_loop(interval=0.02))
    await _wait_until(lambda: h.store.get_run(run.id).status == "completed")
    loop.cancel()
    await asyncio.wait([loop])
    assert len(rounds) >= 2 and all(rounds), "默认自动恢复"


# ---------------------------------------------------------------------- 悬空工具调用
async def test_offline_cancel_pairs_dangling_calls_by_what_is_known(ws, tmp_path):
    """崩溃时一批调用：已发出的记“结果未知”并保留幂等键，排在后面没开始的记“已取消”。"""
    db = tmp_path / "h.db"
    call = {"name": "send_notification", "args": {"to": "ops", "message": "x"}}
    h1 = make_harness(ws, [{"tool_calls": [call, call]}, "好"], store=Store(db))
    h1.tools.get("send_notification").latency = 5
    s = h1.create_session("t")
    run = await h1.start_run(s.id, "发两条")

    def first_started():
        msgs = [m for m in h1.log(s.id).messages() if m.role == "assistant"]
        return bool(msgs) and h1.store.get_step(run.id, msgs[0].tool_calls[0].id) is not None

    await _wait_until(first_started)
    h1._active[run.id].task.cancel()  # 模拟进程崩溃
    await h1.wait(run.id)

    h2 = make_harness(ws, [], store=Store(db), stale_after_seconds=0)
    assert await h2.recover(auto_resume=False) == [run.id]
    assert await h2.cancel(run.id, reason="不需要了") is True
    assert h2.store.get_run(run.id).status == "cancelled"
    started, skipped = _tool_msgs(h2, s.id)
    assert started.details["status"] == "unknown" and "结果未知" in started.text
    assert skipped.details["status"] == "cancelled" and skipped.text == "已取消：不需要了"
    assert h2.store.get_step(run.id, started.tool_call_id).status == "unknown"
    assert h2.store.get_step(run.id, skipped.tool_call_id) is None


# ---------------------------------------------------------------------- 租约围栏
async def test_failed_lease_renewal_fences_the_worker(ws, monkeypatch):
    """读到租约还是自己的，续租却失败（两步之间被接管）：同样按崩溃处理，不再写这个 Run，也不跑 Run 结束 hook。"""
    h = make_harness(ws, [SLOW, "完成"])
    ended = []

    @h.hooks.on(HookEvent.RUN_END)
    async def on_end(inp):
        ended.append(inp.run_id)

    s = h.create_session("t")
    run = await h.start_run(s.id, "甲")
    await _wait_until(lambda: any(m.role == "assistant" for m in h.log(s.id).messages()))
    a = h._active[run.id]
    monkeypatch.setattr(h.store, "heartbeat", lambda *_a: False)
    rec = await asyncio.wait_for(h.wait(run.id), 3)
    assert a.fenced and a.hub.fenced
    assert rec.status == "running" and rec.result is None, "Run 留给新主人，旧 Worker 不写收尾"
    assert h.active_run_of(s.id) is None
    assert ended == []
    assert E.RUN_END not in [e.type for e in h.store.list_events(run.id)]


async def test_child_run_losing_its_lease_stops_cooperatively(ws):
    """子 Run 没有自己的任务，围栏只能走协作式取消：父 Run 拿到 failed 回传后继续，不会被连带终止。"""
    h = make_harness(ws, ScriptedProvider([DELEGATE, SLOW, "父代理：换个办法"], mode="sequential"))
    s = h.create_session("t")
    run = await h.start_run(s.id, "委托")
    await _wait_until(lambda: _child_running(h, run.id) is not None)
    child = _child_running(h, run.id)
    h.store.update_run(child.id, worker_id="w-other")
    rec = await asyncio.wait_for(h.wait(run.id), 5)
    assert rec.status == "completed"
    end = next(e for e in tool_ends(h.store.list_events(run.id)) if e["name"] == "task")
    assert "子代理被取消" in end["content"] and "租约丢失" in end["content"]
    assert h.store.get_run(child.id).status == "cancelled"


async def test_run_in_finishing_phase_is_never_fenced(ws):
    """收尾中状态离开活动集合是自己造成的，不是租约丢失。收尾目前全程同步，心跳插不进来，
    这里直接置位 finishing 钉住这条语义：收尾里将来加了 await，也不能把自己围栏掉。"""
    h = make_harness(ws, [SLOW, "完成"])
    s = h.create_session("t")
    run = await h.start_run(s.id, "甲")
    await _wait_until(lambda: h.store.get_run(run.id).status == "running")
    a = h._active[run.id]
    a.finishing = True
    h.store.update_run(run.id, worker_id="w-other")
    h._fence(a, "迟到的租约丢失")
    await asyncio.sleep(0.3)  # 让心跳至少再醒一次
    assert not a.fenced and not a.hub.fenced and not a.task.done()
    a.finishing = False
    h.store.update_run(run.id, worker_id=h.worker_id)
    await h.cancel(run.id)
    await h.wait(run.id)


# ---------------------------------------------------------------------- 子代理
class _UntrustedFetch(Tool):
    """模拟读取外部内容的工具：结果带污点，不碰网络。"""
    name = "fetch_ticket"
    description = "读取外部工单"
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}, "additionalProperties": False}
    effect = Effect.READ

    async def execute(self, args, ctx):
        return ToolResult("外部工单内容", tainted=True)


CONTRACT = '```json\n{"status": "ok", "summary": "看完了", "findings": [], "artifacts": []}\n```'


async def test_subagent_violating_contract_twice_returns_failed(ws):
    prov = RoleRouter(main=[DELEGATE, "父代理：子任务没成"], sub=["不是 JSON", "还不是 JSON"])
    h = make_harness(ws, prov)
    res = await h.run("委托")
    assert res.status == "completed"
    details = tool_ends(res.events)[0]["details"]
    contract = details["contract"]
    assert contract["status"] == "failed" and "两次均不符合契约" in contract["summary"]
    assert contract["findings"][0]["claim"] == "还不是 JSON", "留下最后一次的原文给父代理判断"
    assert contract["open_questions"], "违约原因要回传"
    assert len(details["child_run_ids"]) == 2


async def test_subagent_ending_without_contract_reports_its_status(ws):
    """子代理预算耗尽、没给出契约：不再要修正，把结束状态如实回传给父代理。"""
    task = {"tool_calls": [{"name": "task", "args": {"description": "查", "prompt": "查", "max_turns": 1}}]}
    prov = RoleRouter(main=[task, "父代理：换个办法"], sub=[{"tool_calls": [{"name": "ls", "args": {}}]}, "还没做完"])
    h = make_harness(ws, prov)
    res = await h.run("委托")
    assert res.status == "completed"
    details = tool_ends(res.events)[0]["details"]
    assert details["contract"]["status"] == "failed"
    assert "子代理以 budget_exhausted 结束" in details["contract"]["summary"]
    assert len(details["child_run_ids"]) == 1, "预算已经用完，不给修正机会"


async def test_child_taint_propagates_to_parent_session(ws):
    """子代理读过不可信内容，它的结论同样不可信：污点要回到父 Run 和父会话。"""
    prov = RoleRouter(main=[DELEGATE, "父代理：收到"], sub=[{"tool_calls": [{"name": "fetch_ticket", "args": {}}]}, CONTRACT])
    h = make_harness(ws, prov)
    h.tools.register(_UntrustedFetch())
    res = await h.run("委托")
    assert res.status == "completed"
    assert tool_ends(res.events)[0]["details"]["contract"]["status"] == "ok"
    assert h.store.get_session(res.session_id).meta.get("tainted") is True


async def test_preempted_parent_leaves_child_in_terminal_state(ws):
    """关闭时父 Run 被抢占式终止：子 Run 也要落到终态，在途调用按“结果未知”配对，不能永远停在 running。"""
    h = make_harness(ws, ScriptedProvider([DELEGATE, SLOW], mode="sequential"))
    s = h.create_session("t")
    run = await h.start_run(s.id, "委托")
    await _wait_until(lambda: _child_running(h, run.id) is not None)
    child = _child_running(h, run.id)
    parent_a, child_a = h._active[run.id], h._active[child.id]

    await h.aclose(grace=0.05)
    await _wait_until(lambda: h._active == {})  # 工具任务只被取消、没有被等待：子 Run 的收尾会晚几个事件循环周期

    assert parent_a.rc.state.get("paused_by") == "shutdown"
    assert "paused_by" not in child_a.rc.state, "关闭只暂停主 Run"
    assert h.store.get_run(run.id).status == "running", "父 Run 留给下次启动接管"
    rec = h.store.get_run(child.id)
    assert rec.status == "cancelled" and "父 Run 被终止" in rec.status_reason
    assert _status_path(h, child.id)[-2:] == [("running", "cancelling"), ("cancelling", "cancelled")]
    [tool] = _tool_msgs(h, child.session_id)
    assert tool.details["status"] in ("unknown", "cancelled")


async def test_preempting_parent_while_child_is_cancelling(ws):
    """子 Run 已在取消中，父 Run 又被抢占：子 Run 不重复迁移到 cancelling，直接落到 cancelled。"""
    h = make_harness(ws, ScriptedProvider([DELEGATE, SLOW], mode="sequential"))
    s = h.create_session("t")
    run = await h.start_run(s.id, "委托")
    await _wait_until(lambda: _child_running(h, run.id) is not None)
    child = _child_running(h, run.id)
    assert await h.cancel(child.id, reason="单独取消")
    h._active[run.id].task.cancel()
    await h.wait(run.id)
    await _wait_until(lambda: h._active == {})
    assert h.store.get_run(child.id).status == "cancelled"
    assert _status_path(h, child.id).count(("running", "cancelling")) == 1


async def test_aclose_abandons_run_that_misses_the_grace_period(ws):
    """宽限期内停不下来的 Run 直接放弃：状态留在 running，下次启动当作崩溃接管。"""
    h = make_harness(ws, [SLOW, "完成"])
    s = h.create_session("t")
    run = await h.start_run(s.id, "甲")
    await _wait_until(lambda: any(m.role == "assistant" for m in h.log(s.id).messages()))
    await h.aclose(grace=0.05)
    assert h.store.get_run(run.id).status == "running"
    assert h.active_run_of(s.id) is None


# ---------------------------------------------------------------------- hook 与杂项
async def test_session_start_hook_context_reaches_model_and_failure_is_recorded(ws):
    """首个 Run 才触发：附加上下文进模型请求，失败的 hook 记成 notice，不悄悄吞掉。"""
    prov = ScriptedProvider(["一", "二"], mode="sequential")
    h = make_harness(ws, prov)
    fired = []

    @h.hooks.on(HookEvent.SESSION_START, priority=1)
    async def add_context(inp):
        fired.append(inp.session_id)
        return HookOutput(additional_context="项目约定：用中文")

    @h.hooks.on(HookEvent.SESSION_START, priority=2)
    async def broken(inp):
        raise RuntimeError("读配置失败")

    s = h.create_session("t")
    first = await h.run("甲", session_id=s.id)
    assert "项目约定：用中文" in prov.requests[0].messages[-1].text
    warned = [e for e in first.events if e.data.get("code") == "hook_warning"]
    assert len(warned) == 1 and "读配置失败" in warned[0].data["text"]
    second = await h.run("乙", session_id=s.id)
    assert fired == [s.id], "只在会话第一个 Run 之前触发一次"
    assert "hook_warning" not in _codes(second.events)


async def test_session_start_hook_without_context_leaves_prompt_untouched(ws):
    prov = ScriptedProvider(["一"], mode="sequential")
    h = make_harness(ws, prov)

    @h.hooks.on(HookEvent.SESSION_START)
    async def quiet(inp):
        return None

    await h.run("原样的提示")
    assert prov.requests[0].messages[-1].text == "原样的提示"


async def test_run_end_hook_runs_after_terminal_state_and_failure_is_recorded(ws):
    """Run 结束 hook 在事件枢纽关闭之后执行：看到的是终态，失败写成 notice，序号接在已有事件后面。"""
    h = make_harness(ws, ["完成"])
    seen = []

    @h.hooks.on(HookEvent.RUN_END)
    async def cleanup(inp):
        seen.append(h.store.get_run(inp.run_id).status)
        raise RuntimeError("清理失败")

    res = await h.run("甲")
    last = lambda: h.store.list_events(res.run_id)[-1].data  # noqa: E731
    await _wait_until(lambda: last().get("code") == "hook_warning")
    assert seen == ["completed"] and "清理失败" in last()["text"]
    events = h.store.list_events(res.run_id)
    seqs = [e.seq for e in events]
    assert seqs == sorted(set(seqs))


async def test_start_connects_mcp_servers_only_once(ws, monkeypatch):
    calls = []

    async def fake_load(servers, **_kw):
        calls.append(servers)
        return [], []

    monkeypatch.setattr("flywhl_harness.tools.mcp.load_mcp_tools", fake_load)
    h = make_harness(ws, [])
    h.config.mcp_servers = {"demo": {}}
    assert await h.start() is h
    assert await h.start() is h
    assert len(calls) == 1


async def test_compact_with_nothing_to_compress_keeps_watermark(ws):
    h = make_harness(ws, [])
    s = h.create_session("t")
    h.store.update_session(s.id, meta={**s.meta, "clear_watermark": 7})
    assert await h.compact(s.id) is None
    assert h.store.get_session(s.id).meta["clear_watermark"] == 7, "没有压缩就不能动水位线"


async def test_branch_starts_new_line_keeps_old_one_and_resets_watermark(ws):
    h = make_harness(ws, ["旧分支答复", "新分支答复"])
    s = h.create_session("t")
    await h.run("旧问题", session_id=s.id)
    old_entries = [e.id for e in h.store.path(s.id)]
    h.store.update_session(s.id, meta={**h.store.get_session(s.id).meta, "clear_watermark": 5})

    h.branch(s.id, None)

    assert h.log(s.id).messages() == [], "回到会话开头"
    assert h.store.get_session(s.id).meta["clear_watermark"] == 0, "水位线是视图下标，换分支必须归零"
    await h.run("新问题", session_id=s.id)
    assert [m.text for m in h.log(s.id).messages() if m.role == "user"] == ["新问题"]
    assert all(h.store.get_entry(i) is not None for i in old_entries), "旧分支保留"
