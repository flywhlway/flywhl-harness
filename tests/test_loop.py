"""Agent Loop：工具调用循环、五种异常情况、并行批次、终止条件、插话与验收。"""

from __future__ import annotations

from flywhl_harness.events import E
from flywhl_harness.llm.base import ModelRequest
from flywhl_harness.policy.hooks import HookEvent, HookOutput
from flywhl_harness.types import StopReason

from .conftest import make_harness, tool_ends


async def test_basic_tool_loop(ws):
    (ws / "a.txt").write_text("hello\n")
    h = make_harness(ws, [
        {"text": "读一下", "tool_calls": [{"name": "read", "args": {"path": "a.txt"}}]},
        "文件内容是 hello。",
    ])
    res = await h.run("读取 a.txt")
    assert res.status == "completed"
    assert res.text == "文件内容是 hello。"
    ends = tool_ends(res.events)
    assert ends[0]["status"] == "ok" and "hello" in ends[0]["content"]
    # 消息历史：user → assistant(tool_call) → tool → assistant
    roles = [m.role for m in h.log(res.session_id).messages()]
    assert roles == ["user", "assistant", "tool", "assistant"]


async def test_five_failure_modes_become_readable_errors(ws):
    """非法工具名、参数不合 schema、参数非法 JSON、工具抛异常——都要转成模型可读的错误，而不是中断 Loop。"""
    from flywhl_harness.tools import tool, default_tools

    @tool
    async def boom() -> str:
        """总是抛异常的工具。"""
        raise RuntimeError("磁盘炸了")

    h = make_harness(ws, [
        {"tool_calls": [
            {"name": "no_such_tool", "args": {}},
            {"name": "read", "args": {"offset": 3}},            # 缺少必填 path
            {"name": "read", "raw": '{"path": "a.txt"'},        # 非法 JSON
            {"name": "boom", "args": {}},
        ]},
        "我看到了这些错误。",
    ])
    h.tools.register(boom)
    res = await h.run("测试错误")
    ends = {e["call_id"]: e for e in tool_ends(res.events)}
    statuses = [e["status"] for e in ends.values()]
    assert statuses == ["invalid", "invalid", "invalid", "error"]
    contents = [e["content"] for e in ends.values()]
    assert "未知工具" in contents[0] and "可用工具" in contents[0]
    assert "缺少必填参数" in contents[1]
    assert "不是合法 JSON" in contents[2]
    assert "磁盘炸了" in contents[3]
    assert res.status == "completed"


async def test_parallel_read_batch_preserves_order(ws):
    for i in range(3):
        (ws / f"f{i}.txt").write_text(f"file {i}\n")
    h = make_harness(ws, [
        {"tool_calls": [{"name": "read", "args": {"path": f"f{i}.txt"}} for i in range(3)]
                       + [{"name": "write", "args": {"path": "out.txt", "content": "x"}}]},
        "好了",
    ])
    res = await h.run("并行读")
    msgs = h.log(res.session_id).messages()
    tool_msgs = [m for m in msgs if m.role == "tool"]
    calls = msgs[1].tool_calls
    # 结果严格按调用的原始顺序写回历史
    assert [m.tool_call_id for m in tool_msgs] == [c.id for c in calls]
    starts = [e for e in res.events if e.type == E.TOOL_START]
    first_end = next(i for i, e in enumerate(res.events) if e.type == E.TOOL_END)
    first_start = next(i for i, e in enumerate(res.events) if e.type == E.TOOL_START)
    # 3 个只读调用在第一个结束之前都已开始（并发）；写操作在只读批之后
    assert sum(1 for e in res.events[first_start:first_end] if e.type == E.TOOL_START) == 3
    assert starts[-1].data["name"] == "write"


async def test_budget_exhausted_wraps_up(ws):
    h = make_harness(ws, [{"tool_calls": [{"name": "ls", "args": {"path": "."}}]}] * 5 + ["总结：做了一些 ls。"],
                     budget__max_turns=2)
    res = await h.run("一直 ls")
    assert res.status == "budget_exhausted"
    msgs = h.log(res.session_id).messages()
    assert any(m.kind == "wrap_up" for m in msgs)
    # 收尾请求禁止调用工具：tool_choice=none 而不是删除工具定义
    wrap_req: ModelRequest = h.provider.requests[-1]
    assert wrap_req.purpose == "wrap_up" and wrap_req.tool_choice == "none" and wrap_req.tools


async def test_loop_guard_stops_dead_loop(ws):
    h = make_harness(ws, [{"tool_calls": [{"name": "ls", "args": {"path": "."}}]}] * 10)
    res = await h.run("重复")
    assert res.status == "loop_detected"
    notices = [e.data for e in res.events if e.type == E.NOTICE and e.data.get("code") == "loop_guard"]
    assert notices, "停止之前应先提醒一次"
    assert any(m.kind == "reminder" and "相同参数" in m.text for m in h.log(res.session_id).messages())


async def test_truncated_tool_calls_are_not_executed(ws):
    h = make_harness(ws, [
        {"tool_calls": [{"name": "write", "args": {"path": "big.txt", "content": "..."}}], "stop_reason": StopReason.MAX_TOKENS},
        "改为分步写入。",
    ])
    res = await h.run("写大文件")
    assert not (ws / "big.txt").exists()
    assert tool_ends(res.events)[0]["status"] == "truncated"
    assert res.status == "completed"


async def test_stop_hook_blocks_then_gives_up_honestly(ws):
    h = make_harness(ws, ["完成了！"] * 6, max_stop_blocks=2)

    @h.hooks.on(HookEvent.STOP)
    async def require_file(inp):
        if not (ws / "report.md").exists():
            return HookOutput(decision="block", reason="report.md 不存在")

    res = await h.run("写报告")
    # 被阻止 2 次后仍未满足：如实以 incomplete 结束，绝不伪装成 completed
    assert res.status == "incomplete"
    feedback = [m for m in h.log(res.session_id).messages() if m.kind == "stop_feedback"]
    assert len(feedback) == 2 and "report.md" in feedback[0].text


async def test_todo_verification(ws):
    todos = [{"content": "a", "status": "completed"}, {"content": "b", "status": "pending"}]
    done = [dict(t, status="completed") for t in todos]
    h = make_harness(ws, [
        {"tool_calls": [{"name": "todo_write", "args": {"todos": todos}}]},
        "好了",                                                   # 想结束，但还有未完成项 → 被阻止
        {"tool_calls": [{"name": "todo_write", "args": {"todos": done}}]},
        "全部完成",
    ])
    res = await h.run("按计划做")
    assert res.status == "completed" and res.text == "全部完成"


async def test_steer_injected_at_safe_point(ws):
    h = make_harness(ws, [
        {"tool_calls": [{"name": "sleep", "args": {"seconds": 0.3}}]},
        lambda req: {"text": "收到插话：" + [m for m in req.messages if m.kind == "steer"][0].text},
    ])
    s = h.create_session("t")
    run = await h.start_run(s.id, "等一下")
    import asyncio
    await asyncio.sleep(0.1)
    assert h.steer(run.id, "顺便看看 README")
    rec = await h.wait(run.id)
    assert rec.status == "completed"
    assert rec.result["text"] == "收到插话：顺便看看 README"
    kinds = [m.kind for m in h.log(s.id).messages()]
    # 插话在工具结果之后、下一次模型调用之前注入，不破坏配对
    assert kinds.index("steer") > [m.role for m in h.log(s.id).messages()].index("tool")


async def test_follow_up_runs_after_natural_stop(ws):
    h = make_harness(ws, ["第一件事做完了", "第二件事也做完了"])
    s = h.create_session("t")
    run = await h.start_run(s.id, "做第一件事")
    h.follow_up(run.id, "再做第二件事")
    rec = await h.wait(run.id)
    assert rec.result["text"] == "第二件事也做完了"


async def test_prefix_stability_append_only(ws):
    """相邻两次请求：后一次的消息列表必须以前一次为前缀（只追加）——前缀缓存与 thinking 签名都依赖它。"""
    h = make_harness(ws, [
        {"tool_calls": [{"name": "ls", "args": {"path": "."}}]},
        {"tool_calls": [{"name": "grep", "args": {"pattern": "x"}}]},
        "完成",
    ])
    await h.run("看看")
    reqs = h.provider.requests
    for a, b in zip(reqs, reqs[1:]):
        assert a.system == b.system and a.tools == b.tools
        assert [m.to_dict() for m in b.messages[: len(a.messages)]] == [m.to_dict() for m in a.messages]


async def test_large_tool_result_is_externalized(ws):
    (ws / "big.log").write_text("\n".join(f"line {i} " + "x" * 80 for i in range(3000)))
    h = make_harness(ws, [
        {"tool_calls": [{"name": "bash", "args": {"command": "cat big.log"}}]},
        lambda req: {"tool_calls": [{"name": "read_ref", "args": {"ref": _ref(req), "offset": 2990, "limit": 5}}]},
        "看到了结尾",
    ], context__tool_result_max_tokens=2000)
    res = await h.run("看日志")
    ends = tool_ends(res.events)
    assert "句柄" in ends[0]["content"]
    assert "line 2993" in ends[1]["content"]   # 第 2990–2994 行（1 起始）即 line 2989..2993


def _ref(req):
    import re
    for m in reversed(req.messages):
        if m.role == "tool":
            found = re.search(r"(blob_[0-9a-f]{16})", m.text)
            if found:
                return found.group(1)
    raise AssertionError("没有找到句柄")


async def test_truncated_text_reply_is_continued(ws):
    h = make_harness(ws, [{"text": "第一部分……", "stop_reason": StopReason.MAX_TOKENS}, "第二部分，完。"])
    res = await h.run("写长文")
    assert res.status == "completed" and res.text == "第二部分，完。"
    assert any(m.kind == "reminder" and "截断" in m.text for m in h.log(res.session_id).messages())


def test_child_budget_does_not_consume_parent_soft_warning():
    from flywhl_harness.agent.budget import Budget
    from flywhl_harness.config import BudgetConfig

    parent = Budget(BudgetConfig(max_turns=10))
    for _ in range(8):
        parent.next_turn()
    child = parent.child(BudgetConfig(max_turns=5))
    assert child.check().state == "ok"
    assert parent.check().state == "soft", "子预算检查上层时不能吃掉上层自己的软提醒"
    for _ in range(2):
        parent.next_turn()
    v = child.check()
    assert v.state == "hard" and v.reasons[0].startswith("上层预算")


async def test_tool_result_records_dynamic_effect(ws):
    """bash 的副作用等级按命令判定（ls 是只读）：结果消息里落盘实际等级，界面重绘时不会显示成“写入”。"""
    h = make_harness(ws, [{"tool_calls": [{"name": "bash", "args": {"command": "ls"}}]}, "完成"])
    res = await h.run("列目录")
    tool_msgs = [m for m in h.log(res.session_id).context().messages if m.role == "tool"]
    assert res.status == "completed" and tool_msgs[0].details["effect"] == "read"


async def test_unpriced_model_warns_that_cost_budget_is_ineffective(ws):
    from flywhl_harness.llm.models import ModelInfo

    h = make_harness(ws, ["好"], budget__max_cost_usd=0.5)
    h.model_info = ModelInfo("some-unpriced-model", "openai")   # 价格表里没有的模型
    res = await h.run("hi")
    assert any(e.type == E.NOTICE and e.data.get("code") == "unpriced_model" for e in res.events)
    h2 = make_harness(ws, ["好"])                                  # 没有配置费用预算：不打扰
    res2 = await h2.run("hi")
    assert not any(e.type == E.NOTICE and e.data.get("code") == "unpriced_model" for e in res2.events)
