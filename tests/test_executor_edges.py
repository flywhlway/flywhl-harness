"""工具执行管线的分支行为：崩溃恢复对账、hook 介入、审批改参、瞬时重试、路径越界与取消收尾。

每条用例都走完整的 Harness，用脚本化模型驱动，断言事件、落库的工具结果和工具实际收到的调用。
bash 载荷在本文件里一律不出现，副作用都由测试内定义的工具或临时工作区里的文件承担。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from flywhl_harness import ScriptedProvider, Store
from flywhl_harness.events import E
from flywhl_harness.policy.approvals import ApprovalDecision
from flywhl_harness.policy.hooks import HookEvent, HookOutput
from flywhl_harness.tools.base import Effect, ToolResult, TransientToolError, tool

from .conftest import make_harness, tool_ends


def notices(events, code: str) -> list[dict]:
    return [e.data for e in events if e.type == E.NOTICE and e.data.get("code") == code]


def tool_messages(h, session_id: str) -> list[Any]:
    return [m for m in h.log(session_id).messages() if m.role == "tool"]


def request_text(req) -> str:
    """把一次模型请求里的全部消息摊平成文本：reminder 注入在消息历史里。"""
    return "\n".join(m.text for m in req.messages)


async def until(cond, tries: int = 300) -> None:
    for _ in range(tries):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("等待条件超时")


# ---------------------------------------------------------------------- 崩溃恢复
CALL_CHARGE = [{"tool_calls": [{"name": "charge", "args": {}}]}, "处理完毕"]


async def crash_inside_tool(ws: Path, db: Path, t, calls: list[str], script: list[Any]):
    """工具已经开始执行时强行取消驱动任务，留下“有 intent 无 result”的现场，再由新 Worker 接管。"""
    h1 = make_harness(ws, script, store=Store(db))
    h1.tools.register(t)
    s = h1.create_session("t")
    run = await h1.start_run(s.id, "扣款")
    await until(lambda: len(calls) == 1)
    h1._active[run.id].task.cancel()
    await asyncio.sleep(0.05)
    h2 = make_harness(ws, script, store=Store(db), stale_after_seconds=0.1)
    h2.tools.register(t)
    await asyncio.sleep(0.3)
    assert await h2.recover(auto_resume=True) == [run.id]
    rec = await h2.wait(run.id)
    return h2, s, rec


async def test_recover_without_reconcile_reports_unknown_instead_of_retrying(ws, tmp_path):
    calls: list[str] = []

    @tool(name="charge", effect=Effect.EXTERNAL)
    async def charge(ctx) -> str:
        """扣款：非幂等、不支持对账。"""
        calls.append(ctx.idempotency_key)
        await asyncio.sleep(30)
        return "已扣款"

    h2, s, rec = await crash_inside_tool(ws, tmp_path / "h.db", charge, calls, CALL_CHARGE)
    assert rec.status == "completed"
    assert len(calls) == 1, "结果未知的非幂等调用不能重放"
    msg = tool_messages(h2, s.id)[0]
    assert msg.details["status"] == "unknown" and msg.is_error
    assert calls[0] in msg.text, "错误信息要带幂等键，方便人工核实"


async def test_recover_idempotent_tool_reruns_with_same_key(ws, tmp_path):
    calls: list[str] = []

    @tool(name="charge", effect=Effect.EXTERNAL, idempotent=True)
    async def charge(ctx) -> str:
        """扣款：幂等，同一幂等键可以重放。"""
        calls.append(ctx.idempotency_key)
        if len(calls) == 1:
            await asyncio.sleep(30)
        return "已扣款"

    h2, s, rec = await crash_inside_tool(ws, tmp_path / "h.db", charge, calls, CALL_CHARGE)
    assert rec.status == "completed"
    assert len(calls) == 2 and calls[0] == calls[1], "重放必须沿用原幂等键"
    assert tool_messages(h2, s.id)[0].details["status"] == "ok"
    codes = [e.data.get("code") for e in h2.store.list_events(rec.id) if e.type == E.NOTICE]
    assert "recover_reexecute" in codes


async def test_recover_reconcile_error_is_returned_as_reconciled_result(ws, tmp_path):
    """对账发现外部状态不一致时，模型读到的是对账结论，工具不会再执行一遍。"""
    calls: list[str] = []

    async def check(args, ctx):
        return ToolResult.error(f"[对账] {ctx.idempotency_key} 与本次参数不一致")

    @tool(name="charge", effect=Effect.EXTERNAL, reconcile=check)
    async def charge(ctx) -> str:
        """扣款：外部系统可查。"""
        calls.append(ctx.idempotency_key)
        await asyncio.sleep(30)
        return "已扣款"

    h2, s, rec = await crash_inside_tool(ws, tmp_path / "h.db", charge, calls, CALL_CHARGE)
    msg = tool_messages(h2, s.id)[0]
    assert len(calls) == 1 and msg.details["status"] == "reconciled" and msg.is_error
    assert "不一致" in msg.text and rec.status == "completed"


async def test_recover_without_intent_reruns_full_pipeline(ws, tmp_path):
    """崩溃发生在审批等待期间：还没写 intent，副作用肯定没发生，接管后按普通调用重走权限和执行。"""
    db = tmp_path / "h.db"
    calls: list[str] = []

    @tool(name="charge", effect=Effect.EXTERNAL)
    async def charge(ctx) -> str:
        """扣款。"""
        calls.append(ctx.idempotency_key)
        return "已扣款"

    h1 = make_harness(ws, CALL_CHARGE, store=Store(db), on_ask="wait")
    h1.tools.register(charge)
    s = h1.create_session("t")
    run = await h1.start_run(s.id, "扣款")
    await until(lambda: h1.store.list_approvals("pending"))
    call_id = h1.log(s.id).messages()[1].tool_calls[0].id
    assert h1.store.get_step(run.id, call_id) is None
    h1._active[run.id].task.cancel()
    await asyncio.sleep(0.05)

    h2 = make_harness(ws, CALL_CHARGE, store=Store(db), stale_after_seconds=0.1)
    h2.tools.register(charge)
    await asyncio.sleep(0.3)
    assert await h2.recover(auto_resume=True) == [run.id]
    rec = await h2.wait(run.id)
    assert rec.status == "completed" and len(calls) == 1
    msg = tool_messages(h2, s.id)[0]
    assert msg.details["status"] == "ok" and h2.store.get_step(run.id, call_id).status == "ok"


# ---------------------------------------------------------------------- PreToolUse hook
WRITE_A = [{"tool_calls": [{"name": "write", "args": {"path": "a.txt", "content": "内容"}}]}, "ok"]


async def test_fail_open_pre_hook_warns_and_call_still_runs(ws):
    h = make_harness(ws, WRITE_A, permissions__mode="bypass")

    @h.hooks.on(HookEvent.PRE_TOOL_USE)
    async def broken(inp):
        raise ValueError("规则文件损坏")

    res = await h.run("x")
    warned = notices(res.events, "hook_warning")
    assert len(warned) == 1 and "规则文件损坏" in warned[0]["text"] and warned[0]["level"] == "warn"
    assert (ws / "a.txt").read_text() == "内容"


async def test_pre_hook_rewrite_failing_schema_never_executes(ws):
    h = make_harness(ws, WRITE_A, permissions__mode="bypass")

    @h.hooks.on(HookEvent.PRE_TOOL_USE)
    async def drop_content(inp):
        return HookOutput(updated_input={"path": "a.txt"})

    res = await h.run("x")
    end = tool_ends(res.events)[0]
    assert end["status"] == "invalid" and end["is_error"] and "未通过校验" in end["content"]
    assert not (ws / "a.txt").exists()


async def test_pre_hook_context_reaches_next_model_call(ws):
    provider = ScriptedProvider(WRITE_A)
    h = make_harness(ws, provider, permissions__mode="bypass")

    @h.hooks.on(HookEvent.PRE_TOOL_USE)
    async def note(inp):
        return HookOutput(additional_context="本仓库的文件头要写版权声明")

    await h.run("x")
    assert "本仓库的文件头要写版权声明" not in request_text(provider.requests[0])
    assert "本仓库的文件头要写版权声明" in request_text(provider.requests[1])


async def test_pre_hook_stop_run_ends_run_with_hook_reason(ws):
    provider = ScriptedProvider(WRITE_A)
    h = make_harness(ws, provider, permissions__mode="bypass")

    @h.hooks.on(HookEvent.PRE_TOOL_USE)
    async def halt(inp):
        return HookOutput(decision="deny", stop_run=True, reason="发现未授权的发布")

    res = await h.run("x")
    assert res.status == "cancelled" and "发现未授权的发布" in res.reason
    assert tool_ends(res.events)[0]["status"] == "denied"
    assert len(provider.requests) == 1, "终止之后不再调用模型"


# ---------------------------------------------------------------------- 审批人改参数
async def resolve_first_approval(h, **decision: Any):
    """启动一个需要审批的写入，批准时带上改过的参数，返回 (会话, Run 记录, 落库事件)。"""
    s = h.create_session("t")
    run = await h.start_run(s.id, "写")
    await until(lambda: h.store.list_approvals("pending"))
    assert h.resolve_approval(h.store.list_approvals("pending")[0].id, True, **decision)
    rec = await h.wait(run.id)
    return s, rec, h.store.list_events(run.id)


async def test_edited_args_failing_schema_are_not_executed(ws):
    h = make_harness(ws, WRITE_A, on_ask="wait")
    s, _, _ = await resolve_first_approval(h, edited_args={"path": "b.txt"})
    msg = tool_messages(h, s.id)[0]
    assert msg.details["status"] == "invalid" and "审批人修改后的参数" in msg.text
    assert not (ws / "a.txt").exists() and not (ws / "b.txt").exists()


async def test_edited_args_hitting_deny_rule_are_refused(ws):
    h = make_harness(ws, WRITE_A, on_ask="wait", permissions__rules=["deny:write(blocked/*)"])
    s, _, _ = await resolve_first_approval(h, edited_args={"path": "blocked/a.txt", "content": "x"})
    msg = tool_messages(h, s.id)[0]
    assert msg.details["status"] == "denied" and "修改后的参数被权限策略拒绝" in msg.text
    assert not (ws / "blocked").exists()


async def test_edited_args_pass_through_silent_pre_hook(ws):
    h = make_harness(ws, WRITE_A, on_ask="wait")
    seen: list[str] = []

    @h.hooks.on(HookEvent.PRE_TOOL_USE)
    async def audit(inp):
        seen.append(inp.tool_input["path"])

    await resolve_first_approval(h, edited_args={"path": "b.txt", "content": "改过"})
    assert seen == ["a.txt", "b.txt"], "审批人改过的参数要再过一次 hook"
    assert (ws / "b.txt").read_text() == "改过" and not (ws / "a.txt").exists()


async def test_edited_args_warning_from_pre_hook_is_published(ws):
    h = make_harness(ws, WRITE_A, on_ask="wait")

    @h.hooks.on(HookEvent.PRE_TOOL_USE)
    async def flaky(inp):
        if inp.tool_input["path"] == "b.txt":
            raise ValueError("二次检查超时")

    _, _, events = await resolve_first_approval(h, edited_args={"path": "b.txt", "content": "改过"})
    assert ["二次检查超时" in n["text"] for n in notices(events, "hook_warning")] == [True]
    assert (ws / "b.txt").read_text() == "改过"


async def test_edited_args_rewritten_by_hook_to_invalid_are_refused(ws):
    h = make_harness(ws, WRITE_A, on_ask="wait")

    @h.hooks.on(HookEvent.PRE_TOOL_USE)
    async def mangle(inp):
        if inp.tool_input["path"] == "b.txt":
            return HookOutput(updated_input={"path": "b.txt"})

    s, _, _ = await resolve_first_approval(h, edited_args={"path": "b.txt", "content": "改过"})
    msg = tool_messages(h, s.id)[0]
    assert msg.details["status"] == "invalid" and "PreToolUse hook 改写后的参数" in msg.text
    assert not (ws / "b.txt").exists()


async def test_edited_args_rewritten_by_hook_execute_with_hook_version(ws):
    h = make_harness(ws, WRITE_A, on_ask="wait")

    @h.hooks.on(HookEvent.PRE_TOOL_USE)
    async def relocate(inp):
        if inp.tool_input["path"] == "b.txt":
            return HookOutput(updated_input={**inp.tool_input, "path": "sandbox/b.txt"})

    s, _, _ = await resolve_first_approval(h, edited_args={"path": "b.txt", "content": "改过"})
    assert tool_messages(h, s.id)[0].details["status"] == "ok"
    assert (ws / "sandbox" / "b.txt").read_text() == "改过" and not (ws / "b.txt").exists()


async def test_custom_approval_backend_without_fatigue_tracking(ws):
    """审批后端是鸭子类型：外部实现只提供 request，不带疲劳统计也要能走通。"""
    h = make_harness(ws, WRITE_A)

    class ExternalBackend:
        on_ask = "wait"

        async def request(self, **kw: Any) -> ApprovalDecision:
            return ApprovalDecision(True, by="ticket:OPS-1")

    h.approvals = ExternalBackend()
    res = await h.run("x")
    assert [e.data["by"] for e in res.events if e.type == E.APPROVAL_RESOLVED] == ["ticket:OPS-1"]
    assert not notices(res.events, "approval_fatigue")
    assert (ws / "a.txt").read_text() == "内容"


# ---------------------------------------------------------------------- PostToolUse hook
async def test_post_hook_redacts_result_for_model_and_log(ws):
    provider = ScriptedProvider(WRITE_A)
    h = make_harness(ws, provider, permissions__mode="bypass")

    @h.hooks.on(HookEvent.POST_TOOL_USE)
    async def redact(inp):
        assert inp.tool_result["is_error"] is False and inp.tool_input["path"] == "a.txt"
        return HookOutput(updated_result="[已脱敏]")

    res = await h.run("x")
    end = tool_ends(res.events)[0]
    assert end["content"] == "[已脱敏]" and not end["is_error"]
    assert tool_messages(h, res.session_id)[0].text == "[已脱敏]"
    assert "[已脱敏]" in request_text(provider.requests[1])


async def test_post_hook_block_marks_result_as_error_with_feedback(ws):
    h = make_harness(ws, WRITE_A, permissions__mode="bypass")

    @h.hooks.on(HookEvent.POST_TOOL_USE)
    async def lint(inp):
        return HookOutput(decision="block", reason="缺少文件头")

    res = await h.run("x")
    end = tool_ends(res.events)[0]
    assert end["is_error"] and "[PostToolUse 反馈]" in end["content"] and "缺少文件头" in end["content"]
    assert (ws / "a.txt").exists(), "block 只影响模型读到的结果，写入已经发生"


async def test_post_hook_warning_and_context_survive_failed_neighbour(ws):
    provider = ScriptedProvider(WRITE_A)
    h = make_harness(ws, provider, permissions__mode="bypass")

    @h.hooks.on(HookEvent.POST_TOOL_USE, priority=1)
    async def broken(inp):
        raise ValueError("审计库不可用")

    @h.hooks.on(HookEvent.POST_TOOL_USE, priority=2)
    async def advise(inp):
        return HookOutput(additional_context="记得更新变更记录")

    res = await h.run("x")
    assert "审计库不可用" in notices(res.events, "hook_warning")[0]["text"]
    assert tool_ends(res.events)[0]["status"] == "ok"
    assert "记得更新变更记录" in request_text(provider.requests[1])


# ---------------------------------------------------------------------- 截止时间与瞬时重试
def scripted_call(name: str = "probe") -> list[Any]:
    return [{"tool_calls": [{"name": name, "args": {}}]}, "ok"]


async def test_wall_deadline_caps_tool_timeout_and_is_visible_to_tool(ws):
    left: list[float | None] = []

    @tool(name="probe", effect=Effect.READ, timeout=300)
    async def probe(ctx) -> str:
        """一直等下去的查询。"""
        left.append(ctx.time_left)
        await asyncio.sleep(60)
        return "不会走到这里"

    h = make_harness(ws, scripted_call(), budget__max_wall_seconds=1.0)
    h.tools.register(probe)
    res = await h.run("x")
    end = tool_ends(res.events)[0]
    assert end["status"] == "timeout" and "（1s）" in end["content"], "工具超时要被剩余墙钟时间压到 1 秒"
    assert left[0] is not None and 0 < left[0] <= 1.0


def flaky_tool(effect: Effect, *, idempotent: bool = False, fail_times: int = 99):
    attempts: list[str] = []

    @tool(name="probe", effect=effect, idempotent=idempotent)
    async def probe(ctx) -> str:
        """前几次抛瞬时错误。"""
        attempts.append(ctx.idempotency_key)
        if len(attempts) <= fail_times:
            raise TransientToolError("连接被重置")
        return "拿到结果"

    return probe, attempts


async def test_transient_error_on_read_tool_retries_then_succeeds(ws):
    probe, attempts = flaky_tool(Effect.READ, fail_times=1)
    h = make_harness(ws, scripted_call())
    h.tools.register(probe)
    res = await h.run("x")
    end = tool_ends(res.events)[0]
    assert end["status"] == "ok" and end["content"] == "拿到结果" and len(attempts) == 2


async def test_transient_error_on_idempotent_external_tool_gives_up_after_three_tries(ws):
    probe, attempts = flaky_tool(Effect.EXTERNAL, idempotent=True)
    h = make_harness(ws, scripted_call())
    h.tools.register(probe)
    res = await h.run("x")
    end = tool_ends(res.events)[0]
    assert end["status"] == "error" and "共尝试 3 次" in end["content"] and "连接被重置" in end["content"]
    assert len(attempts) == 3 and len(set(attempts)) == 1, "重试沿用同一幂等键"


async def test_transient_error_on_non_idempotent_write_is_not_retried(ws):
    probe, attempts = flaky_tool(Effect.WRITE)
    h = make_harness(ws, scripted_call())
    h.tools.register(probe)
    res = await h.run("x")
    end = tool_ends(res.events)[0]
    assert end["status"] == "error" and "非幂等工具不自动重试" in end["content"] and len(attempts) == 1


async def test_transient_retry_skipped_when_deadline_cannot_cover_backoff(ws):
    probe, attempts = flaky_tool(Effect.READ)
    h = make_harness(ws, scripted_call(), budget__max_wall_seconds=1.0)
    h.tools.register(probe)
    res = await h.run("x")
    end = tool_ends(res.events)[0]
    assert end["status"] == "error" and "共尝试 1 次" in end["content"] and len(attempts) == 1


async def test_cancel_during_retry_backoff_stops_further_attempts(ws):
    probe, attempts = flaky_tool(Effect.READ)
    h = make_harness(ws, scripted_call())
    h.tools.register(probe)
    s = h.create_session("t")
    run = await h.start_run(s.id, "x")
    await until(lambda: attempts)
    assert await h.cancel(run.id, "用户不等了")
    rec = await h.wait(run.id)
    assert rec.status == "cancelled" and len(attempts) == 1
    assert tool_messages(h, s.id)[0].is_error, "调用仍要配上一条结果"


# ---------------------------------------------------------------------- 路径越界
async def test_path_escape_from_tool_is_denied_with_workspace_hint(ws):
    outside = ws.parent / "outside.txt"
    h = make_harness(ws, [{"tool_calls": [{"name": "write", "args": {"path": "../outside.txt", "content": "x"}}]}, "ok"],
                     permissions__mode="bypass")
    res = await h.run("x")
    end = tool_ends(res.events)[0]
    assert end["status"] == "denied" and "只能操作工作区内的文件" in end["content"], end
    assert not outside.exists()


async def test_symlink_swapped_to_credential_after_permission_check_is_refused(ws):
    """判定时链接指向普通文件，执行前被换成凭证文件：执行层按真实路径再查一次。"""
    secrets = ws.parent / ".ssh"
    secrets.mkdir()
    (secrets / "id_rsa").write_text("哨兵私钥内容")
    plain = ws / "plain.txt"
    plain.write_text("普通内容")
    link = ws / "link.txt"
    link.symlink_to(plain)
    h = make_harness(ws, [{"tool_calls": [{"name": "read", "args": {"path": "link.txt"}}]}, "ok"],
                     permissions__mode="bypass")

    @h.hooks.on(HookEvent.PRE_TOOL_USE)
    async def swap(inp):
        link.unlink()
        link.symlink_to(secrets / "id_rsa")

    res = await h.run("x")
    end = tool_ends(res.events)[0]
    assert end["status"] == "denied" and "凭证位置" in end["content"], end
    assert "哨兵私钥内容" not in end["content"] and "只能操作工作区内的文件" not in end["content"]


# ---------------------------------------------------------------------- 取消时的收尾
async def test_cancel_between_serial_calls_skips_the_rest(ws):
    ran: list[str] = []
    h = make_harness(ws, [{"tool_calls": [{"name": "stage", "args": {"step": "一"}}, {"name": "stage", "args": {"step": "二"}}]}, "ok"])

    @tool(name="stage", effect=Effect.WRITE)
    async def stage(ctx, step: str) -> str:
        """分步写入，第一步完成时用户叫停。"""
        ran.append(step)
        await h.cancel(ctx.run_id, "用户叫停")
        return f"步骤{step}完成"

    h.tools.register(stage)
    res = await h.run("x")
    ends = tool_ends(res.events)
    assert ran == ["一"] and [e["status"] for e in ends] == ["ok", "skipped"]
    assert "未执行" in ends[1]["content"] and "用户叫停" in ends[1]["content"]
    assert res.status == "cancelled"


async def test_cancel_inside_parallel_batch_keeps_finished_results(ws):
    """并行批里一个调用先做完、另一个在最后安全点被取消：做完的结果不能丢，按原序落盘。"""
    ran: list[str] = []
    fast_finished = asyncio.Event()
    h = make_harness(ws, [{"tool_calls": [{"name": "probe", "args": {"key": "fast"}}, {"name": "probe", "args": {"key": "slow"}}]}, "ok"])

    @tool(name="probe", effect=Effect.READ)
    async def probe(key: str) -> str:
        """只读查询。"""
        ran.append(key)
        return f"{key} 的结果"

    @h.hooks.on(HookEvent.PRE_TOOL_USE)
    async def hold_slow(inp):
        if inp.tool_input["key"] == "slow":
            await fast_finished.wait()
            await h.cancel(inp.run_id, "用户叫停")

    @h.hooks.on(HookEvent.POST_TOOL_USE)
    async def mark_finished(inp):
        fast_finished.set()     # PostToolUse 之后没有挂起点：slow 醒来时 fast 的结果已经写进批次结果表

    h.tools.register(probe)
    res = await h.run("x")
    ends = tool_ends(res.events)
    assert ran == ["fast"], "取消之后不能再发出 slow"
    assert (ends[0]["status"], ends[0]["content"]) == ("ok", "fast 的结果")
    assert ends[1]["status"] == "skipped" and res.status == "cancelled"
    assert [m.details["status"] for m in tool_messages(h, res.session_id)] == ["ok", "skipped"]
