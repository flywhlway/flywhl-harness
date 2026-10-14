"""取消语义：流式中途、可中断工具、不可中断工具、配对不变量、取消与完成的竞态、暂停与恢复。"""

from __future__ import annotations

import asyncio

from flywhl_harness.events import E
from flywhl_harness.types import unpaired_tool_calls

from .conftest import make_harness, tool_ends


async def _wait_for(pred, timeout=5.0):
    t = 0.0
    while not pred():
        await asyncio.sleep(0.02)
        t += 0.02
        assert t < timeout, "等待超时"


async def test_cancel_mid_stream_keeps_partial_text(ws):
    h = make_harness(ws, [{"text": "这是一段很长的回答，" * 40, "delay": 0.01}])
    s = h.create_session("t")
    run = await h.start_run(s.id, "说点什么")
    await asyncio.sleep(0.15)
    assert await h.cancel(run.id, reason="用户不想等了")
    rec = await h.wait(run.id)
    assert rec.status == "cancelled"
    msgs = h.log(s.id).messages()
    partial = msgs[-1]
    assert partial.role == "assistant" and partial.stop_reason == "aborted"
    assert 0 < len(partial.text) < len("这是一段很长的回答，" * 40)


async def test_cancel_interrupts_interruptible_tool_within_one_step(ws):
    h = make_harness(ws, [{"tool_calls": [{"name": "sleep", "args": {"seconds": 30}}, {"name": "ls", "args": {}}]}])
    s = h.create_session("t")
    run = await h.start_run(s.id, "等 30 秒")
    events = []

    async def collect():
        async for ev in h.subscribe(run.id):
            events.append(ev)

    task = asyncio.create_task(collect())
    await _wait_for(lambda: any(e.type == E.TOOL_START for e in events))
    t0 = asyncio.get_running_loop().time()
    await h.cancel(run.id, reason="太久了", by="tester")
    rec = await h.wait(run.id)
    await task
    assert asyncio.get_running_loop().time() - t0 < 2, "取消应在一个安全点内生效"
    assert rec.status == "cancelled"
    by_name = {e["name"]: e["status"] for e in tool_ends(events)}
    assert by_name["sleep"] == "cancelled"   # 并行批的 tool_end 按完成顺序到达，按名字断言
    # 不变量：所有调用都有结果（未执行的也补了合成结果）
    assert not unpaired_tool_calls(h.log(s.id).messages())
    # 状态机迁移合法且可审计：running → cancelling → cancelled，带原因与发起人
    st = [(e.data["from"], e.data["to"]) for e in events if e.type == E.STATUS]
    assert ("running", "cancelling") in st and ("cancelling", "cancelled") in st
    assert "tester" in (rec.status_reason or "")


async def test_non_interruptible_tool_completes_within_grace(ws):
    h = make_harness(ws, [{"tool_calls": [{"name": "send_notification", "args": {"to": "ops", "message": "hi"}}]}],
                     cancel_grace_seconds=5.0)
    h.tools.get("send_notification").latency = 0.5
    s = h.create_session("t")
    run = await h.start_run(s.id, "发通知")
    await asyncio.sleep(0.15)
    await h.cancel(run.id)
    rec = await h.wait(run.id)
    assert rec.status == "cancelled"
    tool_msg = [m for m in h.log(s.id).messages() if m.role == "tool"][0]
    # 已发出的外部请求没有被半途打断：等它完成，结果如实记录
    assert tool_msg.details["status"] == "ok" and tool_msg.details.get("completed_after_cancel")


async def test_non_interruptible_tool_marked_unknown_after_grace(ws):
    h = make_harness(ws, [{"tool_calls": [{"name": "send_notification", "args": {"to": "ops", "message": "hi"}}]}],
                     cancel_grace_seconds=0.1)
    h.tools.get("send_notification").latency = 3
    s = h.create_session("t")
    run = await h.start_run(s.id, "发通知")
    await asyncio.sleep(0.15)
    await h.cancel(run.id)
    await h.wait(run.id)
    tool_msg = [m for m in h.log(s.id).messages() if m.role == "tool"][0]
    assert tool_msg.details["status"] == "unknown" and "幂等键" in tool_msg.text


async def test_cancel_during_approval_wait(ws):
    h = make_harness(ws, [{"tool_calls": [{"name": "write", "args": {"path": "x.txt", "content": "1"}}]}], on_ask="wait")
    s = h.create_session("t")
    run = await h.start_run(s.id, "写文件")
    await _wait_for(lambda: h.store.get_run(run.id).status == "waiting_human")
    assert h.store.list_approvals("pending")
    await h.cancel(run.id)
    rec = await h.wait(run.id)
    assert rec.status == "cancelled"
    assert not h.store.list_approvals("pending"), "取消后待审批对象应失效"
    assert not (ws / "x.txt").exists()


async def test_cancel_after_completion_loses_race(ws):
    h = make_harness(ws, ["完成"])
    res = await h.run("hi")
    assert res.status == "completed"
    assert await h.cancel(res.run_id) is False  # 终态不可变


async def test_pause_and_resume_with_drift_note(ws):
    h = make_harness(ws, [
        {"tool_calls": [{"name": "write", "args": {"path": "a.txt", "content": "v1"}}]},
        {"tool_calls": [{"name": "ls", "args": {}}]},
        lambda req: {"text": "恢复说明：" + next(m.text for m in req.messages if m.kind == "resume_note")},
    ])
    s = h.create_session("t")
    run = await h.start_run(s.id, "写然后看")
    await h.pause(run.id)
    rec = await h.wait(run.id)
    assert rec.status == "paused"
    (ws / "user_added.txt").write_text("用户在暂停期间加的文件")
    await h.resume(run.id)
    rec = await h.wait(run.id)
    assert rec.status == "completed"
    assert "user_added.txt" in rec.result["text"], "恢复时应告诉模型暂停期间工作区的变化"


async def test_budget_and_inbox_survive_pause(ws):
    """回归：暂停恢复后预算继续计数（不能靠反复暂停绕过限额），暂停前排队的消息随恢复带回。"""
    h = make_harness(ws, [{"tool_calls": [{"name": "sleep", "args": {"seconds": 0.3}}]}] * 5 + ["结束"],
                     budget__max_turns=3, loop_guard__stop_repeats=99)
    s = h.create_session("t")
    run = await h.start_run(s.id, "慢慢做")
    await asyncio.sleep(0.1)
    h.follow_up(run.id, "做完记得总结")
    await h.pause(run.id)
    rec = await h.wait(run.id)
    assert rec.status == "paused" and rec.checkpoint["budget"]["turns"] == 1
    assert rec.checkpoint["inbox"]["follow_up"] == ["做完记得总结"]
    await h.resume(run.id)
    rec = await h.wait(run.id)
    assert rec.status == "budget_exhausted", "预算从暂停前的 1 轮继续累计，第 3 轮后耗尽"
    assert rec.result["turns"] >= 3


async def test_external_timeout_is_unknown_not_plain_timeout(ws):
    """非幂等的外部调用超时 ≠ 没发生：报告“结果未知”，提示先核实，而不是鼓励直接重试。"""
    from flywhl_harness.tools.base import Effect, Tool, ToolResult

    class SlowPayment(Tool):
        name = "pay"
        description = "发起付款（外部、非幂等）"
        parameters = {"type": "object", "properties": {}}
        effect = Effect.EXTERNAL
        timeout = 0.2

        async def execute(self, args, ctx):
            await asyncio.sleep(5)
            return ToolResult("已付款")

    class SlowRead(SlowPayment):
        name = "slow_read"
        effect = Effect.READ

    h = make_harness(ws, [{"tool_calls": [{"name": "pay", "args": {}}, {"name": "slow_read", "args": {}}]}, "好"])
    h.tools.register(SlowPayment())
    h.tools.register(SlowRead())
    res = await h.run("付款并查询")
    ends = {e["name"]: e for e in tool_ends(res.events)}
    assert ends["pay"]["status"] == "unknown" and "核实" in ends["pay"]["content"]
    assert ends["slow_read"]["status"] == "timeout"


async def test_external_bash_timeout_is_unknown(ws):
    """bash 自己管理超时；但外部命令（这里用 curl 发 POST）超时后同样是“结果未知”，而不是普通错误。"""
    h = make_harness(ws, [{"tool_calls": [{"name": "bash", "args": {
        "command": "sleep 5; curl -s -X POST http://127.0.0.1:9/hook", "timeout": 1}}]}, "好"])
    res = await h.run("推送")
    end = tool_ends(res.events)[0]
    assert end["status"] == "unknown" and "核实" in end["content"]


async def test_cancelling_parent_marks_task_call_cancelled(ws):
    """父 Run 被取消：子代理协作式停下，task 调用的状态是 cancelled，而不是“执行异常”。"""
    from flywhl_harness import ScriptedProvider

    script = [{"tool_calls": [{"name": "task", "args": {"description": "慢查", "prompt": "等一会儿"}}]},
              {"tool_calls": [{"name": "sleep", "args": {"seconds": 5}}]}]
    h = make_harness(ws, ScriptedProvider(script, mode="sequential"))
    s = h.create_session("t")
    run = await h.start_run(s.id, "委托")
    for _ in range(100):
        if any(r.status == "running" for r in h.store.child_runs(run.id)):
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.1)
    await h.cancel(run.id)
    rec = await h.wait(run.id)
    end = next(e for e in tool_ends(h.store.list_events(run.id)) if e["name"] == "task")
    assert rec.status == "cancelled" and end["status"] == "cancelled"
    assert all(r.status == "cancelled" for r in h.store.child_runs(run.id))
