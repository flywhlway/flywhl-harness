"""真实模型 · 接入层：流式协议、思考开关、缓存、截断、错误分类、降级链、多供应商与 Anthropic 协议。"""

from __future__ import annotations

import asyncio
import os

import pytest

from flywhl_harness import HarnessConfig
from flywhl_harness.cancel import CancelToken
from flywhl_harness.events import E
from flywhl_harness.llm import create_provider
from flywhl_harness.llm.base import ErrorKind, ModelRequest, ProviderError
from flywhl_harness.llm.doctor import check_model
from flywhl_harness.llm.profiles import PROFILES, resolve
from flywhl_harness.types import Message, TextBlock

from .conftest import live_harness, live_model, need, run, tools_used, transcript

pytestmark = pytest.mark.live


def provider_for(spec: str, **cfg):
    need(spec)
    return create_provider(HarnessConfig(model=spec, **{"max_retries": 1, **cfg}))


def user(text: str) -> Message:
    return Message(role="user", content=[TextBlock(text)])


async def stream_all(p, model, messages, **kw):
    evs = [ev async for ev in p.stream(ModelRequest(model=model, system="你是测试助手。", messages=messages, **kw), CancelToken())]
    return evs, evs[-1].message


# ---------------------------------------------------------------------- 协议
async def test_doctor_passes_for_live_model():
    """接入自检：流式文本 + 工具往返（含思考内容回传）。"""
    spec = need(live_model())
    r = await check_model(HarnessConfig(model=spec), spec)
    assert r.ok, (r.error, r.steps)
    assert r.output_tokens > 0 and r.cost_usd >= 0


async def test_thinking_switch_is_honoured():
    p, model = provider_for(live_model(), thinking="disabled")
    evs, off = await stream_all(p, model, [user("1+1 等于几？只回答数字。")], max_tokens=200)
    assert not off.thinking and not any(e.type == "thinking_delta" for e in evs)
    p2, _ = provider_for(live_model(), thinking="enabled")
    _, on = await stream_all(p2, model, [user("9.11 和 9.9 哪个大？只回答数字。")], max_tokens=2000)
    assert on.thinking and "9.9" in on.text
    await p.aclose()
    await p2.aclose()


async def test_prefix_cache_hits_on_repeated_prefix():
    """同一前缀的第二次请求应命中服务端前缀缓存，usage 里能看到 cache_read_tokens。"""
    p, model = provider_for(live_model(), thinking="disabled")
    system = "你是测试助手。以下是一份很长的背景资料，回答时可以忽略。\n" + "背景资料第 {} 段：harness 负责循环、工具与恢复。\n" * 200
    system = system.format(*range(200))
    msgs = [user("只回复 OK")]
    hits = 0
    for _ in range(4):          # 缓存构建需要几秒（尽力而为），最多试几次
        final = None
        async for ev in p.stream(ModelRequest(model=model, system=system, messages=msgs, max_tokens=20), CancelToken()):
            if ev.type == "done":
                final = ev.message
        hits = final.usage.cache_read_tokens
        if hits:
            break
        await asyncio.sleep(3)
    await p.aclose()
    assert hits > 1000, "重复前缀没有命中缓存"


async def test_max_tokens_truncation_is_reported():
    p, model = provider_for(live_model(), thinking="disabled")
    _, msg = await stream_all(p, model, [user("从 1 数到 300，用逗号分隔。")], max_tokens=40)
    await p.aclose()
    assert msg.stop_reason == "max_tokens"


async def test_error_classification_on_real_endpoints():
    profile, _ = resolve(live_model())
    p, _ = provider_for(f"{profile.name}:no-such-model-xyz")
    with pytest.raises(ProviderError) as ei:
        await stream_all(p, "no-such-model-xyz", [user("hi")], max_tokens=5)
    assert ei.value.kind == ErrorKind.NOT_FOUND
    await p.aclose()
    bad, model = create_provider(HarnessConfig(model=live_model(), api_key="sk-invalid-for-test", max_retries=1))
    with pytest.raises(ProviderError) as ei:
        await stream_all(bad, model, [user("hi")], max_tokens=5)
    assert ei.value.kind == ErrorKind.AUTH
    await bad.aclose()


# ---------------------------------------------------------------------- 降级链
async def test_fallback_from_missing_model_to_live_model(ws):
    """主模型不存在（NOT_FOUND）→ 沿降级链切到可用模型，Run 正常完成并留下 fallback 提示。"""
    profile, model = resolve(need(live_model()))
    h = live_harness(ws, model=f"{profile.name}:no-such-model-xyz", fallback_models=[f"{profile.name}:{model}"])
    async with h:
        res = await run(h, "只回复：降级成功", label="降级链：模型不存在")
    assert res.status == "completed", transcript(h, res.session_id)
    assert any(e.type == E.NOTICE and e.data.get("code") == "fallback" for e in res.events)
    assert "降级成功" in res.text


async def test_cross_provider_fallback_on_bad_key(ws):
    """主供应商密钥失效（AUTH）→ 跨供应商降级到另一家。"""
    other = need("dashscope:qwen3.8-flash")
    h = live_harness(ws, api_key="sk-invalid-for-test", fallback_models=[other])
    async with h:
        res = await run(h, "只回复：换家成功", label="跨供应商降级")
    assert res.status == "completed", transcript(h, res.session_id)
    served = [m for m in h.log(res.session_id).context().messages if m.role == "assistant"][-1]
    assert served.provider == "dashscope" and "换家成功" in res.text


# ---------------------------------------------------------------------- 多供应商：每家完整跑一次多步任务
@pytest.mark.parametrize("spec", ["dashscope:qwen3.8-flash", "kimi:kimi-k2.6", "minimax:MiniMax-M2.7",
                                  "openrouter:deepseek/deepseek-v4.1-flash"])
async def test_agent_task_on_other_providers(ws, spec):
    (ws / "a.txt").write_text("flywhl 的秘密数字是 4721\n", encoding="utf-8")
    h = live_harness(ws, model=need(spec))
    async with h:
        res = await run(h, "读取 a.txt，告诉我秘密数字。", label=f"多供应商 {spec.split(':')[0]}")
    assert res.status == "completed", transcript(h, res.session_id)
    assert "read" in tools_used(res) and "4721" in res.text


@pytest.mark.parametrize("label,base_url,key_env,model", [
    ("Claude 经 OpenRouter", "https://openrouter.ai/api", "OPENROUTER_API_KEY", "anthropic:claude-haiku-5-5"),
    ("DeepSeek Anthropic 端点", "https://api.deepseek.com/anthropic", "DEEPSEEK_API_KEY", "anthropic:deepseek-flash"),
])
async def test_anthropic_protocol_agent_task(ws, label, base_url, key_env, model):
    """Anthropic Messages 协议的完整多步任务：tool_result 合并、thinking 签名原样回传、前缀缓存。"""
    (ws / "a.txt").write_text("flywhl 的秘密数字是 4721\n", encoding="utf-8")
    (ws / "b.txt").write_text("另一个数字是 1024\n", encoding="utf-8")
    h = live_harness(ws, model=model, base_url=base_url, api_key_env=key_env)
    async with h:
        res = await run(h, "先列出目录，再读取 a.txt 和 b.txt，把两个数字相加告诉我结果。", label=label)
    assert res.status == "completed", transcript(h, res.session_id)
    assert "5745" in res.text.replace(",", ""), transcript(h, res.session_id)
    assert {"ls", "read"} <= set(tools_used(res))
