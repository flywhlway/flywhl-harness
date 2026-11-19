"""模型正常结束回合，最后一条消息却只有思考（或什么都没有）：先追问一次，仍然没有正文就以 incomplete 收尾。

2026-11-18 发版前的基准回归 r4：deepseek-flash 的 curl 被出站白名单拦下后，最后一条消息 ``finish_reason=stop``，
只有思考，没有正文和工具调用。Run 记成 completed，用户看到的最后一句是更早一轮的“我先取一下数据。”，
拿不到“为什么没做”的说明。这条消息本来就不会回传（见 ``llm/base.py`` 的 ``has_payload``），追问时要说明那段思考不会保留。
"""

from __future__ import annotations

import pytest

from flywhl_harness.events import E
from flywhl_harness.llm.base import prepare_history
from flywhl_harness.observe.replay import replay_run
from flywhl_harness.policy.hooks import HookEvent
from flywhl_harness.types import MsgKind

from .conftest import make_harness
from .test_truncation_recovery import FakeDeepSeek, _harness, _invalid_assistants, _sse

ANSWER = "curl 被出站白名单拦下了，api.example.com 不在 network_allow 里，数据没取到。"


def _notices(res, code: str) -> list[dict]:
    return [e.data for e in res.events if e.type == E.NOTICE and e.data.get("code") == code]


async def test_thinking_only_end_turn_is_nudged_once_and_the_answer_completes_the_run(ws):
    fake = FakeDeepSeek([
        _sse(thinking="请求被拦了，换个办法也不行，算了……", finish="stop"),   # 正常结束回合，却只有思考
        _sse(text=ANSWER),
    ])
    h = _harness(ws, fake)
    res = await h.run("取一下接口数据")
    assert res.status == "completed" and res.text == ANSWER
    assert len(fake.bodies) == 2 and not _notices(res, "empty_reply")
    second = fake.bodies[1]
    assert not _invalid_assistants(second)
    assert [m["role"] for m in second["messages"]] == ["system", "user", "user"]   # 只有思考的那条不回传
    hint = second["messages"][-1]["content"]
    assert "只有思考" in hint and "不会保留" in hint and "卡在哪" in hint
    # 不是截断：不走续写那套，输出上限也不提高
    assert "从中断处继续" not in hint and [b["max_tokens"] for b in fake.bodies] == [8192, 8192]
    nudges = [m for m in h.log(res.session_id).messages() if m.kind == MsgKind.REMINDER]
    assert len(nudges) == 1


async def test_thinking_only_twice_ends_incomplete_with_a_notice(ws):
    fake = FakeDeepSeek([_sse(thinking="想" * 50, finish="stop") for _ in range(3)])
    h = _harness(ws, fake)
    res = await h.run("取一下接口数据")
    assert res.status == "incomplete" and res.text == ""
    assert "没有输出正文" in (h.store.get_run(res.run_id).result or {}).get("reason", "")
    assert len(fake.bodies) == 2                    # 只追问一次
    assert len(_notices(res, "empty_reply")) == 1
    assert all(not _invalid_assistants(b) for b in fake.bodies)


async def test_an_empty_reply_is_nudged_without_mentioning_thinking(ws):
    h = make_harness(ws, ["", "做完了。"])
    res = await h.run("x")
    assert res.status == "completed" and res.text == "做完了。"
    hint = h.provider.requests[1].messages[-1].text
    assert "回复是空的" in hint and "思考" not in hint


async def test_truncation_continuations_and_the_empty_reply_nudge_are_counted_separately(ws):
    """先被截断（续写计数），再只有思考就结束回合（追问计数）：两套计数互不占用。"""
    h = make_harness(ws, [{"thinking": "想" * 20, "stop_reason": "max_tokens"}, {"thinking": "想"}, "结论"])
    res = await h.run("x")
    assert res.status == "completed" and res.text == "结论"
    hints = [m.text for m in h.log(res.session_id).messages() if m.kind == MsgKind.REMINDER]
    assert len(hints) == 2 and "思考用完了单次输出的长度上限" in hints[0] and "只有思考就结束了回合" in hints[1]


async def test_stop_hook_sees_the_answer_not_the_empty_reply(ws):
    """追问在验收之前：Stop hook 只看到追问之后的正文，没有为那条空回复跑一遍。"""
    h = make_harness(ws, [{"thinking": "想"}, ANSWER])
    seen: list[str] = []

    @h.hooks.on(HookEvent.STOP)
    async def record(inp):
        seen.append(inp.last_message)

    res = await h.run("x")
    assert res.status == "completed" and seen == [ANSWER]


async def test_queued_input_takes_precedence_over_the_nudge(ws):
    """排队的追加任务先处理：模型接着回应新输入，不再追问那条空回复。"""
    h = make_harness(ws, [{"thinking": "想", "delay_before": 0.2}, "第二件做完了"])
    s = h.create_session("t")
    run = await h.start_run(s.id, "开始")
    assert h.follow_up(run.id, "再做第二件")
    rec = await h.wait(run.id)
    assert rec.status == "completed"
    kinds = [m.kind for m in h.log(s.id).messages() if m.role == "user"]
    assert MsgKind.REMINDER not in kinds and MsgKind.FOLLOW_UP in kinds


@pytest.mark.parametrize("script, status", [
    ([{"tool_calls": [{"name": "read", "args": {"path": "a.txt"}}]}, {"thinking": "读到了，但是……"}, ANSWER], "completed"),
    ([{"tool_calls": [{"name": "read", "args": {"path": "a.txt"}}]}, {"thinking": "想"}, {"thinking": "还是想"}], "incomplete"),
])
async def test_the_nudge_replays_deterministically(ws, script, status):
    """追问是 harness 按录制的回复算出来的，回放时重算：上下文指纹、工具序列和结局都与录制一致。"""
    (ws / "a.txt").write_text("1\n")
    h = make_harness(ws, script)
    res = await h.run("读 a.txt 然后汇报")
    assert res.status == status
    sent = prepare_history(h.provider.requests[2].messages)
    assert sent[-1].kind == MsgKind.REMINDER and sent[-2].role == "tool"   # 空回复不回传，追问紧跟工具结果
    rep = await replay_run(h.store, res.run_id, config=h.config, base_tools=h.tools)
    assert rep.divergence is None, rep.notes
    assert rep.same_tool_sequence and rep.same_final_text and rep.status == rep.original_status == status
    assert rep.deterministic
