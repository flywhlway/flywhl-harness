"""策略层的边角行为：审批代理的超时与并发决定、hook 总线的结果聚合、无法解析路径时的权限判定。

审批用例直接驱动 ApprovalBroker 和真实 Store（内存库）；hook 用例驱动 HookBus；
权限用例用真实工作区和内置工具，路径都只做静态判定，不会真的打开。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from flywhl_harness import Store
from flywhl_harness.cancel import CancelToken
from flywhl_harness.config import HarnessConfig
from flywhl_harness.policy.approvals import ApprovalBroker, ApprovalDecision
from flywhl_harness.policy.hooks import (
    Hook,
    HookBus,
    HookEvent,
    HookInput,
    HookOutput,
    normalize_decision,
)
from flywhl_harness.policy.permissions import Decision, PermissionPolicy
from flywhl_harness.runtime.workspace import Workspace
from flywhl_harness.tools import default_tools


# ---------------------------------------------------------------------- 审批代理
@pytest.fixture
def store() -> Store:
    return Store(":memory:")


async def ask(broker: ApprovalBroker, **kw: Any) -> ApprovalDecision:
    session = broker.store.create_session("审批")
    run = broker.store.create_run(session.id, "p")
    return await broker.request(run_id=run.id, session_id=session.id, call_id="c1", tool="write",
                                args={"path": "a.txt"}, summary="写 a.txt", risk="medium", reason="写操作需要确认",
                                cancel=CancelToken(), **kw)


async def test_unanswered_approval_expires_and_rejects(store):
    broker = ApprovalBroker(store, timeout=0.05)
    dec = await ask(broker)
    assert not dec.approved and dec.by == "system:timeout" and "默认拒绝" in dec.reason
    assert store.list_approvals("pending") == [] and len(store.list_approvals("expired")) == 1


async def test_timeout_loses_to_decision_written_by_another_worker(store):
    """另一个 Worker 通过共享数据库批准了审批，本进程的 future 收不到：超时时 CAS 失败，以库里的决定为准。"""
    broker = ApprovalBroker(store, timeout=0.05)

    async def decided_elsewhere(rec) -> None:
        assert store.resolve_approval(rec.id, "approved", decided_by="user:alice", reason="看过了",
                                      edited_args={"path": "b.txt"}, remember=True)

    dec = await ask(broker, on_created=decided_elsewhere)
    assert dec.approved and dec.by == "user:alice" and dec.reason == "看过了"
    assert dec.edited_args == {"path": "b.txt"} and dec.remember


async def test_lost_approval_record_rejects(store):
    """审批行被外部清理：取不到决定就按拒绝处理，绝不当作批准。"""
    broker = ApprovalBroker(store, timeout=0.05)

    async def wipe(rec) -> None:
        store._x("DELETE FROM approvals WHERE id=?", (rec.id,))

    dec = await ask(broker, on_created=wipe)
    assert not dec.approved and dec.by == "system" and "记录丢失" in dec.reason


async def test_failing_resolver_rejects_with_reason(store):
    async def closed_terminal(rec) -> ApprovalDecision:
        raise EOFError("终端已关闭")

    broker = ApprovalBroker(store, timeout=5, resolver=closed_terminal)
    dec = await ask(broker)
    assert not dec.approved and dec.by == "system" and "终端已关闭" in dec.reason
    assert len(store.list_approvals("rejected")) == 1


async def test_hanging_resolver_is_cancelled_after_timeout(store):
    stopped: list[bool] = []

    async def never_answers(rec) -> ApprovalDecision:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            stopped.append(True)
            raise
        return ApprovalDecision(True)

    broker = ApprovalBroker(store, timeout=0.05, resolver=never_answers)
    dec = await ask(broker)
    await asyncio.sleep(0)   # 让被取消的 resolver 任务走完
    assert not dec.approved and dec.by == "system:timeout" and stopped == [True]
    assert broker._waiters == {}, "等待表不能留下已结束的审批"


# ---------------------------------------------------------------------- hook 总线
def post_input(content: str = "原文") -> HookInput:
    return HookInput(HookEvent.POST_TOOL_USE, "r", "s", tool_name="read", tool_input={"path": "a"},
                     tool_result={"content": content, "is_error": False, "details": None})


def test_to_json_renames_tool_result_for_command_hooks():
    d = post_input().to_json()
    assert d["hook_event_name"] == HookEvent.POST_TOOL_USE
    assert d["tool_response"]["content"] == "原文" and "tool_result" not in d and "event" not in d


def test_to_json_keeps_empty_tool_input_but_drops_other_empty_fields():
    d = HookInput(HookEvent.PRE_TOOL_USE, "r", "s", tool_name="noop", tool_input={}).to_json()
    assert d["tool_input"] == {} and "cwd" not in d and "data" not in d and "tool_response" not in d


async def test_add_names_anonymous_hook_after_its_function():
    async def audit_trail(inp):
        raise ValueError("写入失败")

    bus = HookBus()
    hook = bus.add(Hook(HookEvent.POST_TOOL_USE, audit_trail))
    out = await bus.run(HookEvent.POST_TOOL_USE, post_input())
    assert hook.name == "audit_trail" and "hook audit_trail 失败" in out.warnings[0]


async def test_updated_result_chains_to_later_hooks():
    bus = HookBus()
    seen: list[str] = []

    @bus.on(HookEvent.POST_TOOL_USE, priority=1)
    async def mask(inp):
        return HookOutput(updated_result=inp.tool_result["content"] + "+脱敏")

    @bus.on(HookEvent.POST_TOOL_USE, priority=2)
    async def tag(inp):
        seen.append(inp.tool_result["content"])
        return HookOutput(updated_result=inp.tool_result["content"] + "+标记")

    inp = post_input()
    out = await bus.run(HookEvent.POST_TOOL_USE, inp)
    assert seen == ["原文+脱敏"] and out.updated_result == "原文+脱敏+标记"
    assert inp.tool_result["content"] == "原文+脱敏+标记" and inp.tool_result["is_error"] is False


async def test_updated_result_without_tool_result_is_still_aggregated():
    """事件本身不带工具结果（比如 Stop）时，hook 的 updated_result 只进聚合结果，不能因为无处写回而报错。"""
    bus = HookBus()

    @bus.on(HookEvent.STOP)
    async def summarize(inp):
        return HookOutput(updated_result="总结")

    inp = HookInput(HookEvent.STOP, "r", "s")
    out = await bus.run(HookEvent.STOP, inp)
    assert out.updated_result == "总结" and inp.tool_result is None


@pytest.mark.parametrize("reason, want", [("发布窗口已关闭", "发布窗口已关闭"), ("", "要求终止运行")])
async def test_stop_run_is_aggregated_with_reason(reason, want):
    bus = HookBus()

    @bus.on(HookEvent.POST_TOOL_USE)
    async def halt(inp):
        return HookOutput(stop_run=True, reason=reason)

    out = await bus.run(HookEvent.POST_TOOL_USE, post_input())
    assert out.stop_run and out.reason == f"[halt] {want}"


@pytest.mark.parametrize("event", [HookEvent.PRE_TOOL_USE, HookEvent.POST_TOOL_USE])
async def test_misspelled_decision_warns_instead_of_vanishing(event):
    bus = HookBus()

    @bus.on(event)
    async def typo(inp):
        return HookOutput(decision="blok")

    out = await bus.run(event, HookInput(event, "r", "s", tool_name="read", tool_input={"path": "a"}))
    assert out.decision is None
    assert len(out.warnings) == 1 and "[typo]" in out.warnings[0] and "'blok'" in out.warnings[0]


@pytest.mark.parametrize("event, raw, want", [
    (HookEvent.STOP, "deny", "block"),
    (HookEvent.STOP, "continue", None),
    (HookEvent.PRE_TOOL_USE, " Approve ", "allow"),
])
def test_normalize_decision_across_events(event, raw, want):
    assert normalize_decision(event, raw) == (want, None)


# ---------------------------------------------------------------------- 无法解析的路径
BAD_PATH = "a\x00b.txt"   # NUL 字符：路径解析直接抛异常，文件系统上不可能存在


@pytest.fixture
def tools():
    return default_tools(HarnessConfig())


def test_unresolvable_path_is_treated_as_outside_workspace(tmp_path, tools):
    ws = Workspace(tmp_path, snapshots=False)
    read_verdict = PermissionPolicy("default", workspace=ws).evaluate(tools.get("read"), {"path": BAD_PATH})
    assert read_verdict.decision == Decision.ASK and "工作区之外" in read_verdict.reason
    edits = PermissionPolicy("accept_edits", workspace=ws)
    write_verdict = edits.evaluate(tools.get("write"), {"path": BAD_PATH, "content": "x"})
    assert write_verdict.decision == Decision.ASK, "解析不了的路径不能按工作区内自动接受"


def test_unresolvable_path_still_matches_rules_on_raw_target(tmp_path, tools):
    ws = Workspace(tmp_path, snapshots=False)
    p = PermissionPolicy("bypass", ["deny:write(a*)"], workspace=ws)
    assert p.evaluate(tools.get("write"), {"path": BAD_PATH, "content": "x"}).decision == Decision.DENY


def test_unresolvable_path_is_never_auto_approved(tmp_path, tools):
    ws = Workspace(tmp_path, snapshots=False)
    p = PermissionPolicy("default", workspace=ws, auto_approve=["write(**)"])
    write = tools.get("write")
    args = {"path": BAD_PATH, "content": "x"}
    rule, why = p.auto_approval(write, args, p.evaluate(write, args))
    assert rule is None and "工作区之外" in why
