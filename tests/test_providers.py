"""模型接入层的离线测试：输入是各家真实接口录下来的 SSE 流与错误响应（tests/fixtures/sse/）。

真实模型测试（tests/live/）验证“现在接得通”；这里验证“解析逻辑对已知的各家差异是对的”，
不花钱、确定性、能进 CI。录制方法见 tests/live/README.md。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from flywhl_harness import HarnessConfig
from flywhl_harness.cancel import CancelToken
from flywhl_harness.config import load_dotenv
from flywhl_harness.llm import ResilientProvider, ScriptedProvider, create_provider
from flywhl_harness.llm.base import ErrorKind, ModelRequest, ProviderError, classify_http_error
from flywhl_harness.llm.openai_compat import OpenAICompatProvider, ThinkTagSplitter, to_openai_messages
from flywhl_harness.llm.profiles import PROFILES, resolve, split_spec
from flywhl_harness.runtime.retry import RetryPolicy
from flywhl_harness.types import Message, TextBlock, ThinkingBlock, ToolCall

FIX = Path(__file__).parent / "fixtures" / "sse"
KEY_ENVS = {e for p in PROFILES.values() for e in p.key_env} | {"OPENAI_BASE_URL", "OLLAMA_BASE_URL"}


@pytest.fixture
def no_keys(monkeypatch):
    for k in KEY_ENVS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def provider_on(profile: str, body: str, *, status: int = 200, seen: list | None = None) -> OpenAICompatProvider:
    """用录制的响应体替换网络：请求体记录在 seen 里，便于断言“发出去的是什么”。"""
    p = OpenAICompatProvider(api_key="test", profile=profile)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(status, content=body.encode(), headers={"content-type": "text/event-stream"})

    p.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return p


async def collect(p: OpenAICompatProvider, model: str, messages: list[Message] | None = None) -> tuple[list, Message]:
    req = ModelRequest(model=model, system="s", messages=messages or [Message(role="user", content=[TextBlock("q")])],
                       tools=[{"name": "get_weather", "description": "d", "parameters": {"type": "object"}}])
    evs = [ev async for ev in p.stream(req, CancelToken())]
    await p.aclose()
    return evs, evs[-1].message


# ====================================================================== 真实流解析
@pytest.mark.parametrize("profile,model", [
    ("deepseek", "deepseek-flash"), ("dashscope", "qwen3.8-flash"), ("kimi", "kimi-k2.6"),
    ("minimax", "MiniMax-M2.7"), ("openrouter", "deepseek/deepseek-v4.1-flash"),
])
async def test_recorded_tool_call_streams(profile, model):
    evs, msg = await collect(provider_on(profile, (FIX / f"{profile}_tool_calls.sse").read_text()), model)
    calls = msg.tool_calls
    # 两个并行调用：参数按 index 增量拼接，id 只在首个分片出现
    assert [c.name for c in calls] == ["get_weather", "get_weather"]
    assert {c.arguments["city"] for c in calls} == {"北京", "上海"}
    assert all(c.parse_error is None and c.id for c in calls)
    assert msg.stop_reason == "tool_use"
    # 每家都有思考内容，但来源不同：reasoning_content / reasoning / 正文内联的 <think>
    assert msg.thinking and any(e.type == "thinking_delta" for e in evs)
    assert "<think>" not in msg.text and "</think>" not in msg.text
    assert msg.usage.input_tokens > 0 and msg.usage.output_tokens > 0
    assert msg.provider == profile


async def test_recorded_final_answers_and_usage():
    _, ds = await collect(provider_on("deepseek", (FIX / "deepseek_final.sse").read_text()), "deepseek-flash")
    assert "北京" in ds.text and ds.stop_reason == "end_turn"
    assert ds.usage.cache_read_tokens > 0          # 第二轮请求命中了前缀缓存（prompt_cache_hit_tokens）
    assert ds.usage.cost_usd > 0                   # 按价格表计价，不再是 $0
    _, orr = await collect(provider_on("openrouter", (FIX / "openrouter_final.sse").read_text()),
                           "deepseek/deepseek-v4.1-flash")
    reported = next(json.loads(line[5:])["usage"]["cost"] for line in (FIX / "openrouter_final.sse").read_text().splitlines()
                    if line.startswith("data:") and '"cost"' in line)
    assert orr.usage.cost_usd == pytest.approx(reported)   # OpenRouter 返回实际费用，以它为准


async def test_served_model_is_recorded():
    body = (FIX / "deepseek_final.sse").read_text()
    _, msg = await collect(provider_on("deepseek", body), "deepseek-chat")    # 旧模型名
    assert msg.model == "deepseek-flash"                                         # 记录实际服务的模型


# ====================================================================== 思考内容回传
def _history(provider: str) -> list[Message]:
    call = ToolCall("call_1", "get_weather", {"city": "北京"})
    return [
        Message(role="user", content=[TextBlock("天气？")]),
        Message(role="assistant", content=[ThinkingBlock("先查北京", None), TextBlock("我来查询。"), call],
                stop_reason="tool_use", provider=provider),
        Message(role="tool", content=[TextBlock("晴")], tool_call_id="call_1", tool_name="get_weather"),
    ]


def test_reasoning_echo_styles():
    ds = to_openai_messages("s", _history("deepseek"), echo="field", provider="deepseek")[2]
    assert ds["reasoning_content"] == "先查北京" and ds["content"] == "我来查询。"
    mm = to_openai_messages("s", _history("minimax"), echo="inline", provider="minimax")[2]
    assert mm["content"].startswith("<think>\n先查北京\n</think>") and "reasoning_content" not in mm
    qw = to_openai_messages("s", _history("dashscope"), echo="none", provider="dashscope")[2]
    assert "reasoning_content" not in qw and "<think>" not in qw["content"]
    # 降级后别家产生的思考不回传：对当前供应商没有意义
    other = to_openai_messages("s", _history("dashscope"), echo="field", provider="deepseek")[2]
    assert other["reasoning_content"] == ""


async def test_deepseek_request_echoes_reasoning_and_maps_thinking_switch():
    seen: list = []
    p = provider_on("deepseek", (FIX / "deepseek_final.sse").read_text(), seen=seen)
    p.options = PROFILES["deepseek"].request_options("disabled", "high")
    await collect(p, "deepseek-flash", _history("deepseek"))
    body = seen[0]
    assert body["messages"][2]["reasoning_content"] == "先查北京"
    assert body["thinking"] == {"type": "disabled"} and "reasoning_effort" not in body   # 关闭思考时不发强度
    assert body["stream_options"] == {"include_usage": True}


def test_thinking_switch_translation():
    assert PROFILES["deepseek"].request_options("enabled", "max") == {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}
    assert PROFILES["dashscope"].request_options("disabled", None) == {"enable_thinking": False}
    assert PROFILES["openrouter"].request_options("enabled", "low") == {"reasoning": {"enabled": True, "effort": "low"}}
    assert PROFILES["minimax"].request_options("disabled", "high") == {}   # 不支持的开关直接忽略


def test_think_tag_splitter_handles_split_tags():
    def run(chunks):
        sp, out = ThinkTagSplitter(), []
        for c in chunks:
            out += sp.feed(c)
        out += sp.flush()
        return "".join(t for k, t in out if k == "thinking"), "".join(t for k, t in out if k == "text")

    assert run(["<", "thi", "nk>\n先想", "想</th", "ink>\n\n", "\n答案"]) == ("先想想", "答案")
    assert run(["正文里讨论 <think> 标签"]) == ("", "正文里讨论 <think> 标签")   # 只处理开头的思考块
    assert run(["<thinking> 不是标签"]) == ("", "<thinking> 不是标签")
    assert run(["<think>没写完"]) == ("没写完", "")


# ====================================================================== 流的完整性与错误
async def test_stream_without_finish_reason_is_a_network_error():
    lines = [ln for ln in (FIX / "deepseek_final.sse").read_text().splitlines() if '"finish_reason":"stop"' not in ln
             and "[DONE]" not in ln]
    with pytest.raises(ProviderError) as ei:
        await collect(provider_on("deepseek", "\n".join(lines[:len(lines) // 2]) + "\n"), "deepseek-flash")
    assert ei.value.kind == ErrorKind.NETWORK and ei.value.retryable    # 交给重试层，而不是当作正常结束


async def test_mid_stream_error_and_insufficient_resource():
    err = 'data: {"error": {"code": 429, "message": "Rate limit exceeded"}}\n\n'
    with pytest.raises(ProviderError) as ei:
        await collect(provider_on("openrouter", err), "x")
    assert ei.value.kind == ErrorKind.RATE_LIMIT
    res = ('data: {"choices":[{"delta":{"content":"半截"},"finish_reason":null}]}\n\n'
           'data: {"choices":[{"delta":{},"finish_reason":"insufficient_system_resource"}]}\n\ndata: [DONE]\n\n')
    with pytest.raises(ProviderError) as ei:
        await collect(provider_on("deepseek", res), "deepseek-flash")
    assert ei.value.kind == ErrorKind.OVERLOADED


@pytest.mark.parametrize("label,kind", [
    ("deepseek_bad_model", ErrorKind.NOT_FOUND),        # DeepSeek 用 400 表示模型不存在
    ("deepseek_bad_key", ErrorKind.AUTH),
    ("dashscope_model_not_found", ErrorKind.NOT_FOUND),
    ("kimi_bad_model", ErrorKind.NOT_FOUND),
    ("zhipu_insufficient_balance", ErrorKind.BILLING),  # 智谱把余额不足包装成 429：不能当限流去重试
    ("kimi_context_overflow", ErrorKind.CONTEXT_OVERFLOW),
])
def test_recorded_errors_are_classified(label, kind):
    e = json.loads((FIX / "errors.json").read_text())[label]
    err = classify_http_error(e["status"], e["body"])
    assert err.kind == kind
    assert err.retryable is (kind in (ErrorKind.RATE_LIMIT,))


async def test_http_error_from_stream_endpoint():
    e = json.loads((FIX / "errors.json").read_text())["zhipu_insufficient_balance"]
    with pytest.raises(ProviderError) as ei:
        await collect(provider_on("zhipu", e["body"], status=e["status"]), "glm-5.3-flash")
    assert ei.value.kind == ErrorKind.BILLING and ei.value.status == 429


# ====================================================================== 模型写法与降级链
def test_model_spec_resolution(no_keys):
    assert split_spec("deepseek:deepseek-flash") == ("deepseek", "deepseek-flash")
    assert split_spec("ollama:qwen3.5:9b-mlx") == ("ollama", "qwen3.5:9b-mlx")   # 只按第一个冒号切
    assert split_spec("qwen3.5:9b-mlx") == (None, "qwen3.5:9b-mlx")              # 前缀不是供应商：整体是模型名
    assert resolve("deepseek-flash")[0].name == "deepseek"                        # 模型表登记了供应商
    assert resolve(None)[0].name == "mock"                                        # 没有任何密钥：演示模型
    no_keys.setenv("DEEPSEEK_API_KEY", "k")
    no_keys.setenv("ANTHROPIC_API_KEY", "k")
    assert resolve(None) == (PROFILES["deepseek"], "deepseek-flash")              # 项目默认：DeepSeek flash
    assert resolve(None, "openai")[0].name == "deepseek"                          # 兼容旧配置：只有 DeepSeek 密钥
    with pytest.raises(ValueError):
        resolve(None, "nope")


def test_create_provider_builds_cross_provider_chain(no_keys):
    no_keys.setenv("DEEPSEEK_API_KEY", "k1")
    no_keys.setenv("DASHSCOPE_API_KEY", "k2")
    cfg = HarnessConfig(model="deepseek:deepseek-flash", base_url="https://proxy.example/v1",
                        fallback_models=["deepseek:deepseek-v4-pro", "dashscope:qwen3.8-flash"])
    rp, model = create_provider(cfg)
    assert model == "deepseek-flash" and isinstance(rp, ResilientProvider)
    (p0, m0), (p1, m1), (p2, m2) = rp.chain
    assert p0 is p1 and p0.base_url == "https://proxy.example/v1"            # 同一家复用同一个客户端
    assert p2.name == "dashscope" and p2.base_url.startswith("https://dashscope")   # base_url 覆盖只属于主模型
    assert (m0, m1, m2) == ("deepseek-flash", "deepseek-v4-pro", "qwen3.8-flash")


def test_env_aliases_for_model_and_fallbacks(no_keys):
    no_keys.setenv("LLM_PRIMARY_MODEL", "deepseek:deepseek-flash")
    no_keys.setenv("LLM_FALLBACK_MODEL", "dashscope:qwen3.8-flash, kimi:kimi-k2.6")
    cfg = HarnessConfig.load(path="/nonexistent.toml")
    assert cfg.model == "deepseek:deepseek-flash"
    assert cfg.fallback_models == ["dashscope:qwen3.8-flash", "kimi:kimi-k2.6"]
    no_keys.setenv("FLYWHL_MODEL", "kimi:kimi-k2.6")                         # FLYWHL_* 优先
    assert HarnessConfig.load(path="/nonexistent.toml").model == "kimi:kimi-k2.6"


async def _drain(rp):
    return [ev async for ev in rp.stream(ModelRequest(model="m", system="", messages=[]), CancelToken())]


async def test_fallback_on_billing_and_cross_provider_auth():
    broke = ScriptedProvider([ProviderError(ErrorKind.BILLING, "余额不足")], mode="sequential")
    other = ScriptedProvider(["别家回答"], mode="sequential")
    evs = await _drain(ResilientProvider([(broke, "a"), (other, "b")], RetryPolicy(max_attempts=3, base_delay=0.001)))
    assert evs[-1].message.text == "别家回答" and broke.calls == 1       # 余额不足不重试，直接换
    # 鉴权失败：同一家换模型没用（直接报错），换到另一家才降级
    bad = ScriptedProvider([ProviderError(ErrorKind.AUTH, "401")] * 3, mode="sequential")
    with pytest.raises(ProviderError):
        await _drain(ResilientProvider([(bad, "a"), (bad, "b")], RetryPolicy(max_attempts=3, base_delay=0.001)))
    good = ScriptedProvider(["换家成功"], mode="sequential")
    evs = await _drain(ResilientProvider([(bad, "a"), (good, "b")], RetryPolicy(max_attempts=3, base_delay=0.001)))
    assert evs[-1].message.text == "换家成功"


def test_anthropic_server_fallback_only_on_first_party():
    pytest.importorskip("anthropic")
    from flywhl_harness.llm.anthropic import AnthropicProvider

    req = ModelRequest(model="claude-sonnet-5-5", system="s", messages=[Message(role="user", content=[TextBlock("q")])])
    official = AnthropicProvider(api_key="k", base_url="https://api.anthropic.com")
    kw, betas = official._kwargs(req)
    assert kw["fallbacks"] == "default" and betas == ["server-side-fallback-2026-07-01"]
    gateway = AnthropicProvider(api_key="k", base_url="https://openrouter.ai/api")   # 实测：网关对该参数返回 400
    kw, betas = gateway._kwargs(req)
    assert "fallbacks" not in kw and betas == []


def test_load_dotenv_does_not_override(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text('# 注释\nFLYWHL_T_A=1\nexport FLYWHL_T_B="带 空格"\nFLYWHL_T_C=v # 行尾注释\nFLYWHL_T_KEEP=new\n')
    monkeypatch.setenv("FLYWHL_T_KEEP", "old")
    for k in ("FLYWHL_T_A", "FLYWHL_T_B", "FLYWHL_T_C"):
        monkeypatch.delenv(k, raising=False)
    loaded = load_dotenv(f)
    import os

    assert loaded == ["FLYWHL_T_A", "FLYWHL_T_B", "FLYWHL_T_C"]
    assert (os.environ["FLYWHL_T_B"], os.environ["FLYWHL_T_C"], os.environ["FLYWHL_T_KEEP"]) == ("带 空格", "v", "old")
    for k in loaded:
        monkeypatch.delenv(k)
