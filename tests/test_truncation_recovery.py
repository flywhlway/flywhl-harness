"""思考阶段被 max_tokens 截断之后：不能把只有思考的消息发回去（DeepSeek 400），会话要能接着跑，输出上限要提高。

2026-11-14 的真实故障：deepseek-flash 打算一次写完一份 HTML 报告，思考用完了 8192 的输出上限，消息里只有思考。
它进了历史，下一次请求是 ``content=null`` 且没有 ``tool_calls``，DeepSeek 返回
``400 Invalid assistant message: content or tool_calls must be set``；之后重试、发“继续任务”都带着它，会话永久失败。
这里用一个按 DeepSeek 同样规则校验请求体的假端点重放整条链路（真实接口上的复现见提交说明）。
"""

from __future__ import annotations

import json

import httpx

from flywhl_harness import Harness, HarnessConfig, Store
from flywhl_harness.events import E
from flywhl_harness.llm import ResilientProvider, ScriptedProvider
from flywhl_harness.llm.base import ModelRequest, prepare_history
from flywhl_harness.llm.openai_compat import OpenAICompatProvider, to_openai_messages
from flywhl_harness.runtime.retry import RetryPolicy
from flywhl_harness.cancel import CancelToken
from flywhl_harness.types import Message, ServerBlock, StopReason, TextBlock, ThinkingBlock, ToolCall

INVALID = "Invalid assistant message: content or tool_calls must be set"


def _chunk(delta: dict, finish: str | None = None, usage: dict | None = None) -> str:
    body = {"id": "x", "object": "chat.completion.chunk", "model": "deepseek-flash",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], "usage": usage}
    return "data: " + json.dumps(body, ensure_ascii=False) + "\n\n"


def _sse(*, thinking: str = "", text: str = "", finish: str = "stop", out: int = 10) -> str:
    parts = []
    if thinking:
        parts.append(_chunk({"role": "assistant", "content": None, "reasoning_content": thinking}))
    if text:
        parts.append(_chunk({"content": text}))
    parts.append(_chunk({}, finish, {"prompt_tokens": 100, "completion_tokens": out}))
    return "".join(parts) + "data: [DONE]\n\n"


class FakeDeepSeek:
    """按 DeepSeek 的规则校验请求：assistant 消息既没有 content 也没有 tool_calls 就 400。"""

    def __init__(self, replies: list[str | httpx.Response]):
        self.replies = list(replies)
        self.bodies: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        for m in body["messages"]:
            if m["role"] == "assistant" and m.get("content") is None and not m.get("tool_calls"):
                err = {"error": {"message": f"{INVALID} (request_id: test)", "type": "invalid_request_error",
                                 "param": None, "code": "invalid_request_error"}}
                return httpx.Response(400, json=err)
        reply = self.replies.pop(0)
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, content=reply.encode(), headers={"content-type": "text/event-stream"})


def _harness(ws, fake: FakeDeepSeek, **over) -> Harness:
    cfg = HarnessConfig.load(workspace=str(ws))
    cfg.permissions.on_ask = "allow"
    cfg.snapshots = False
    cfg.trust_workspace = True
    for k, v in over.items():
        setattr(cfg, k, v)
    p = OpenAICompatProvider(api_key="test", profile="deepseek")
    p.client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return Harness(cfg, provider=p, model="deepseek-flash", store=Store(":memory:"))


def _invalid_assistants(body: dict) -> list[dict]:
    return [m for m in body["messages"] if m["role"] == "assistant" and m.get("content") is None and not m.get("tool_calls")]


async def test_thinking_only_truncation_is_not_sent_back_and_the_run_goes_on(ws):
    fake = FakeDeepSeek([
        _sse(thinking="先规划报告结构……" * 50, finish="length", out=8192),   # 思考用完了上限：没有正文、没有工具调用
        _sse(text="报告已完成。"),
    ])
    h = _harness(ws, fake)
    res = await h.run("写一份调研报告")
    assert res.status == "completed" and res.text == "报告已完成。"
    second = fake.bodies[1]
    assert not _invalid_assistants(second)
    assert [m["role"] for m in second["messages"]] == ["system", "user", "user"]   # 只有思考的那条不回传
    hint = second["messages"][-1]["content"]
    assert "没有输出正文" in hint and "从中断处继续" not in hint                    # 没有“中断处”可接，换一句提示
    # 思考也算在 max_tokens 里：原样重来会在同一个地方再被截断，之后的调用提高到 4 倍
    assert fake.bodies[0]["max_tokens"] == 8192 and second["max_tokens"] == 32768
    raised = [e.data for e in res.events if e.type == E.NOTICE and e.data.get("code") == "max_tokens_raised"]
    assert len(raised) == 1 and "32768" in raised[0]["text"]
    # 落盘的历史保持原样（只追加），过滤只发生在发出请求时
    kept = [m for m in h.log(res.session_id).messages() if m.role == "assistant"]
    assert kept[0].stop_reason == StopReason.MAX_TOKENS and kept[0].thinking and not kept[0].text


async def test_a_session_poisoned_by_the_old_code_recovers_on_retry(ws):
    """修复前落盘的会话：历史里已经有只有思考的消息、一条 400 的错误消息和“继续任务”。修复后重试直接能跑。"""
    fake = FakeDeepSeek([_sse(text="接着完成了。")])
    h = _harness(ws, fake)
    s = h.create_session("旧会话")
    log = h.log(s.id)
    call = ToolCall("call_1", "ls", {"path": "."})
    for m in [
        Message(role="user", content=[TextBlock("写报告")], meta={"kind": "prompt"}),
        Message(role="assistant", content=[ThinkingBlock("先列目录", None), call], stop_reason=StopReason.TOOL_USE,
                provider="deepseek", model="deepseek-flash"),
        Message(role="tool", content=[TextBlock("a.txt")], tool_call_id="call_1", tool_name="ls"),
        Message(role="assistant", content=[ThinkingBlock("很长的思考" * 100, None)], stop_reason=StopReason.MAX_TOKENS,
                provider="deepseek", model="deepseek-flash"),
        Message(role="user", content=[TextBlock("<system-reminder>\n截断了\n</system-reminder>")], meta={"kind": "reminder"}),
        Message(role="assistant", content=[], stop_reason=StopReason.ERROR, error=f"[bad_request HTTP 400] {INVALID}"),
    ]:
        log.append_message(m, None)
    res = await h.run("继续任务", session_id=s.id)
    assert res.status == "completed" and res.text == "接着完成了。"
    body = fake.bodies[0]
    assert not _invalid_assistants(body)
    assert body["messages"][2]["reasoning_content"] == "先列目录"   # 带工具调用的那条照常回传思考
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "tool", "user", "user"]


async def test_the_retry_button_skips_the_thinking_only_message_too(ws):
    """页面上的“重试”走 retry()：失败的 Run 从落盘历史接着跑，历史里那条只有思考的消息同样不发。"""
    other = httpx.Response(400, json={"error": {"message": "something else is wrong", "type": "invalid_request_error"}})
    fake = FakeDeepSeek([_sse(thinking="先规划……" * 50, finish="length", out=8192), other, _sse(text="重试后完成。")])
    h = _harness(ws, fake)
    s = h.create_session("t")
    run = await h.start_run(s.id, "写报告")
    failed = await h.wait(run.id)
    assert failed.status == "failed"
    await h.retry(run.id)
    rec = await h.wait(run.id)
    assert rec.status == "completed" and len(fake.bodies) == 3
    assert not _invalid_assistants(fake.bodies[2])


async def test_repeated_thinking_only_truncation_ends_incomplete_not_completed(ws):
    fake = FakeDeepSeek([_sse(thinking="想" * 200, finish="length", out=8192) for _ in range(4)])
    h = _harness(ws, fake)
    res = await h.run("写报告")
    assert res.status == "incomplete" and "思考阶段" in (h.store.get_run(res.run_id).result or {}).get("reason", "")
    # 8192 → 32768 → 65536 封顶，不超过模型登记的最大输出
    assert [b["max_tokens"] for b in fake.bodies] == [8192, 32768, 65536, 65536]
    assert all(not _invalid_assistants(b) for b in fake.bodies)


async def test_unregistered_models_keep_the_configured_limit(ws):
    """没登记最大输出的模型按默认 8192 算：不往上加，免得超过服务端上限变成另一种 400。"""
    fake = FakeDeepSeek([_sse(thinking="想", finish="length", out=8192), _sse(text="好")])
    h = _harness(ws, fake)
    h.model = "some-private-model"
    h.model_info = h.provider.model_info("some-private-model")
    res = await h.run("x")
    assert res.status == "completed"
    assert [b["max_tokens"] for b in fake.bodies] == [8192, 8192]


def test_prepare_history_drops_only_messages_with_nothing_to_send():
    think_only = Message(role="assistant", content=[ThinkingBlock("想", None)], stop_reason=StopReason.MAX_TOKENS)
    empty_text = Message(role="assistant", content=[TextBlock(""), ThinkingBlock("想", None)])
    server_only = Message(role="assistant", content=[ServerBlock({"type": "server_tool_use", "id": "srv_1"}, "h")])
    text = Message(role="assistant", content=[ThinkingBlock("想", None), TextBlock("答")])
    user = Message(role="user", content=[TextBlock("问")])
    kept = prepare_history([user, think_only, empty_text, server_only, text])
    assert kept == [user, server_only, text]   # 供应商侧块对 Anthropic 有意义，留给它自己判断
    out = to_openai_messages("s", [user, server_only, text], echo="field", provider="deepseek")
    assert [m["role"] for m in out] == ["system", "user", "assistant"] and out[-1]["content"] == "答"


async def test_fallback_model_gets_the_raised_limit_clamped_to_its_own_max_output():
    seen: list[int] = []

    class Recorder(ScriptedProvider):
        async def stream(self, req, cancel):
            seen.append(req.max_tokens)
            async for ev in super().stream(req, cancel):
                yield ev

    from flywhl_harness.llm.base import ErrorKind, ProviderError

    broke = ScriptedProvider([ProviderError(ErrorKind.BILLING, "余额不足")], mode="sequential")
    backup = Recorder(["好"], mode="sequential")   # model_info 未登记：最大输出按默认 8192
    rp = ResilientProvider([(broke, "deepseek-flash"), (backup, "small")], RetryPolicy(max_attempts=1, base_delay=0.001))
    req = ModelRequest(model="deepseek-flash", system="", messages=[], max_tokens=32768, metadata={"max_tokens_base": 8192})
    [ev async for ev in rp.stream(req, CancelToken())]
    assert seen == [8192]
    seen.clear()
    plain = ModelRequest(model="deepseek-flash", system="", messages=[], max_tokens=16000)   # 用户自己配的值：原样发
    rp2 = ResilientProvider([(ScriptedProvider([ProviderError(ErrorKind.BILLING, "x")], mode="sequential"), "a"),
                             (Recorder(["好"], mode="sequential"), "small")], RetryPolicy(max_attempts=1, base_delay=0.001))
    [ev async for ev in rp2.stream(plain, CancelToken())]
    assert seen == [16000]
