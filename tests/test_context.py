"""上下文管理：切点规则、压缩条目与上下文视图、自动压缩、旧结果清理、只追加模型的保护。"""

from __future__ import annotations

from dataclasses import replace

from flywhl_harness import ScriptedProvider
from flywhl_harness.agent.compaction import find_cut_index
from flywhl_harness.agent.context import ContextManager
from flywhl_harness.config import ContextConfig
from flywhl_harness.events import E
from flywhl_harness.llm.models import get_model_info
from flywhl_harness.types import Message, TextBlock, ThinkingBlock, ToolCall, tool_result_message, user_message

from .conftest import make_harness


def _turn(i: int, big: int = 200) -> list[Message]:
    call = ToolCall(f"c{i}", "read", {"path": f"f{i}"})
    return [
        user_message(f"第 {i} 个问题"),
        Message(role="assistant", content=[TextBlock("读一下"), call]),
        tool_result_message(call, "x" * big),
        Message(role="assistant", content=[TextBlock(f"回答 {i}")]),
    ]


def test_cut_never_lands_on_tool_result():
    msgs = [m for i in range(10) for m in _turn(i, big=2000)]
    for keep in (100, 500, 1000, 3000, 6000):
        j = find_cut_index(msgs, keep)
        assert j is None or msgs[j].role != "tool"
        if j is not None:  # 保留段里每个工具结果都能找到它的调用
            kept = msgs[j:]
            ids = {c.id for m in kept for c in m.tool_calls}
            assert all(m.tool_call_id in ids for m in kept if m.role == "tool")


def test_cut_prefers_user_boundary():
    msgs = [m for i in range(10) for m in _turn(i)]
    j = find_cut_index(msgs, 300)
    assert msgs[j].role == "user"


async def test_auto_compaction_in_run(ws):
    script = []
    for i in range(8):
        script.append({"tool_calls": [{"name": "bash", "args": {"command": f"python3 -c \"print('{i}' * 3000)\""}}]})
    script.append("完成")
    # 压缩会减少历史中的 assistant 数量，所以脚本按调用次数（sequential）而非 assistant 数推进
    h = make_harness(ws, ScriptedProvider(script, mode="sequential"), context__context_window=8000, context__keep_recent_tokens=1500,
                     context__reserve_tokens=1000, loop_guard__stop_repeats=99,
                     context__clear_old_tool_results=False)  # 关掉“旧结果清理”，单独验证压缩
    res = await h.run("打印很多东西")
    assert res.status == "completed"
    comp = [e for e in res.events if e.type == E.COMPACTION_END and not e.data.get("skipped")]
    assert comp, "水位超过阈值应触发压缩"
    assert comp[0].data["tokens_after"] < comp[0].data["tokens_before"]
    view = h.log(res.session_id).context()
    assert view.messages[0].kind == "compaction" and "打印很多东西" in view.messages[0].text, "原始任务原文必须保留"
    # 压缩后的视图仍满足配对不变量
    ids = {c.id for m in view.messages for c in m.tool_calls}
    assert all(m.tool_call_id in ids for m in view.messages if m.role == "tool")


async def test_thinking_stripped_from_retained_segment(ws):
    h = make_harness(ws, ["x"])
    s = h.create_session("t")
    log = h.log(s.id)
    for i in range(6):
        log.append_message(user_message(f"q{i} " + "内容" * 400))
        log.append_message(Message(role="assistant", content=[ThinkingBlock("想想", "sig"), TextBlock(f"a{i}")],
                                   provider="anthropic"))
    res = await h.compact(s.id)
    assert res is not None
    view = log.context()
    # 保留段的 thinking 签名是在完整历史下生成的，摘要替换历史后必须去掉
    kept_before = [m for m in view.messages[1:] if m.role == "assistant"]
    assert all(not m.thinking for m in kept_before)


def test_clearing_disabled_for_append_only_models():
    cfg = ContextConfig(context_window=1000, clear_threshold=0.1, keep_tool_results_turns=1)
    msgs = [m for i in range(5) for m in _turn(i, big=3000)]
    normal = ContextManager(cfg, replace(get_model_info("x"), append_only=False))
    stats = normal.estimate(msgs, "", [])
    cleared, wm = normal.apply_clearing(msgs, stats, 0)
    assert wm > 0 and any("已清理" in m.text for m in cleared if m.role == "tool")
    strict = ContextManager(cfg, get_model_info("claude-opus-5-5"))
    same, wm2 = strict.apply_clearing(msgs, strict.estimate(msgs, "", []), 0)
    assert wm2 == 0 and same is msgs, "要求只追加的模型不做改写历史的清理"


async def test_clearing_happens_before_compaction(ws):
    """从轻到重：水位先越过清理阈值时只清理旧工具结果，不必动用有损的摘要压缩。"""
    script = [{"tool_calls": [{"name": "bash", "args": {"command": f"python3 -c \"print('{i}' * 3000)\""}}]} for i in range(8)]
    h = make_harness(ws, script + ["完成"], context__context_window=8000, context__keep_recent_tokens=1500,
                     context__reserve_tokens=1000, loop_guard__stop_repeats=99)
    res = await h.run("打印很多东西")
    codes = [e.data.get("code") for e in res.events if e.type == E.NOTICE]
    assert "clear_tool_results" in codes


def test_estimate_uses_real_usage():
    from flywhl_harness.types import Usage
    cm = ContextManager(ContextConfig(), get_model_info("x"))
    msgs = _turn(0)
    msgs[3].usage = Usage(input_tokens=5000, output_tokens=10)
    msgs.append(user_message("再问一句"))
    st = cm.estimate(msgs, "sys", [])
    assert st.source == "usage+estimate" and 5010 < st.tokens < 5100


async def test_plan_survives_compaction(ws):
    """回归：最后一次 todo_write 被压缩进摘要后，计划仍保留在摘要中，Stop 验收仍然生效。"""
    todos = [{"content": "写报告", "status": "in_progress"}]
    script = [{"tool_calls": [{"name": "todo_write", "args": {"todos": todos}}]}]
    script += [{"tool_calls": [{"name": "bash", "args": {"command": f"python3 -c \"print('{i}' * 3000)\""}}]} for i in range(6)]
    script += ["我做完了", {"tool_calls": [{"name": "todo_write", "args": {"todos": [dict(todos[0], status="completed")]}}]}, "真的做完了"]
    h = make_harness(ws, ScriptedProvider(script, mode="sequential"), context__context_window=8000,
                     context__keep_recent_tokens=1500, context__reserve_tokens=1000, loop_guard__stop_repeats=99,
                     context__clear_old_tool_results=False)
    res = await h.run("按计划写报告")
    assert any(e.type == E.COMPACTION_END and not e.data.get("skipped") for e in res.events)
    view = h.log(res.session_id).context()
    assert "## 当前计划" in view.messages[0].text and "写报告" in view.messages[0].text
    # 计划被压缩进摘要之后，“我做完了”仍然会被验收拦下，直到计划更新为完成
    assert any(m.kind == "stop_feedback" for m in h.log(res.session_id).messages())
    assert res.status == "completed" and res.text == "真的做完了"


async def test_compaction_usage_is_charged(ws):
    from flywhl_harness.types import Usage

    h = make_harness(ws, ["x"])
    s = h.create_session("t")
    for i in range(6):
        h.log(s.id).append_message(user_message("内容" * 400))
    from flywhl_harness.agent.compaction import Compactor
    from flywhl_harness.cancel import CancelToken
    res = await Compactor(h.provider, h.model, h.config.context).compact(
        h.log(s.id), h.log(s.id).context(), CancelToken(), tokens_before=4000, force=True)
    assert isinstance(res.usage, Usage) and res.usage.output_tokens > 0
