"""模型接入自检（``flywhl doctor``）：用最小的真实调用确认“这家服务按 harness 的方式接得通”。

每个模型跑两项检查，都绕过重试层直接调用 provider，好让错误分类原样暴露出来：

1. **流式文本**：能鉴权、能流式返回、usage 与费用能算出来；
2. **工具往返**：模型发起一次工具调用（参数是合法 JSON）→ 回填结果 → 模型基于结果作答。
   这一步覆盖了各家差异最大的地方：tool_calls 分片拼接、tool 消息格式、思考内容回传。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from ..cancel import CancelToken
from ..config import HarnessConfig
from ..types import Message, TextBlock
from .base import ModelRequest, Provider, ProviderError
from .profiles import PROFILES, resolve

_SYSTEM = "你是模型接入自检助手。严格按用户要求作答，不要多说。"
_TOOL = {"name": "lookup_code", "description": "按名字查询内部编号。",
         "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "要查询的名字"}},
                        "required": ["name"]}}
_CODE = "ALPHA-7319"


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""
    seconds: float = 0.0


@dataclass
class CheckResult:
    spec: str
    label: str
    steps: list[Step] = field(default_factory=list)
    served_model: str = ""
    thinking_chars: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.steps) and all(s.ok for s in self.steps)


async def _call(p: Provider, model: str, messages: list[Message], tools: list | None) -> tuple[Message, float]:
    t0 = time.monotonic()
    first: float | None = None
    final: Message | None = None
    async for ev in p.stream(ModelRequest(model=model, system=_SYSTEM, messages=messages, tools=tools or [],
                                          max_tokens=2048), CancelToken()):
        if first is None and ev.type in ("text_delta", "thinking_delta", "toolcall_start"):
            first = time.monotonic() - t0
        if ev.type == "done":
            final = ev.message
    assert final is not None
    return final, first or 0.0


async def check_model(cfg: HarnessConfig, spec: str, *, timeout: float = 90.0) -> CheckResult:
    from . import _make

    profile, model = resolve(spec, cfg.provider)
    res = CheckResult(f"{profile.name}:{model}", profile.label)
    # 配置里的 base_url / api_key 只属于主模型的供应商
    p = _make(profile, cfg, own_endpoint=resolve(cfg.model, cfg.provider)[0].name == profile.name)

    def account(m: Message) -> None:
        res.served_model = m.model or model
        res.thinking_chars += len(m.thinking)
        if m.usage:
            res.input_tokens += m.usage.input_tokens
            res.output_tokens += m.usage.output_tokens
            res.cache_read_tokens += m.usage.cache_read_tokens
            res.cost_usd += m.usage.cost_usd

    try:
        async with asyncio.timeout(timeout):
            t0 = time.monotonic()
            m, ttft = await _call(p, model, [Message(role="user", content=[TextBlock("请只回复：flywhl-ok")])], None)
            account(m)
            ok = "flywhl-ok" in m.text.lower()
            res.steps.append(Step("流式文本", ok, f"首个分片 {ttft:.1f}s" if ok else f"回复不符合预期：{m.text[:80]!r}",
                                  time.monotonic() - t0))

            t0 = time.monotonic()
            history = [Message(role="user", content=[TextBlock("调用 lookup_code 查询 name=alpha 的编号，然后只回复查到的编号。")])]
            m, _ = await _call(p, model, history, [_TOOL])
            account(m)
            calls = m.tool_calls
            if not calls or calls[0].parse_error or calls[0].name != "lookup_code":
                why = "没有发起工具调用" if not calls else (calls[0].parse_error or f"调用了 {calls[0].name}")
                res.steps.append(Step("工具往返", False, why, time.monotonic() - t0))
                return res
            history.append(m)
            for c in calls:
                history.append(Message(role="tool", content=[TextBlock(_CODE)], tool_call_id=c.id, tool_name=c.name))
            m, _ = await _call(p, model, history, [_TOOL])
            account(m)
            ok = _CODE in m.text or _CODE.split("-")[1] in m.text
            res.steps.append(Step("工具往返", ok, f"参数 {calls[0].arguments}" if ok else f"没有用上工具结果：{m.text[:80]!r}",
                                  time.monotonic() - t0))
    except ProviderError as e:
        res.error = str(e)
    except TimeoutError:
        res.error = f"超过 {timeout:.0f}s 未完成"
    except Exception as e:  # 自检要把任何异常都报告出来，而不是中断其它供应商的检查
        res.error = f"{type(e).__name__}: {e}"
    finally:
        await p.aclose()
    return res


def default_specs(cfg: HarnessConfig) -> list[str]:
    """默认检查：当前配置的主模型 + 降级链 + 每个配置了密钥的供应商的默认模型（去重）。"""
    specs: list[str] = []
    for spec in [cfg.model, *cfg.fallback_models]:
        if spec:
            p, m = resolve(spec, cfg.provider)
            specs.append(f"{p.name}:{m}")
    for p in PROFILES.values():
        if p.kind != "mock" and p.needs_key and p.api_key():
            specs.append(f"{p.name}:{p.default_model}")
    return list(dict.fromkeys(specs))


async def check_all(cfg: HarnessConfig, specs: list[str] | None = None, *, timeout: float = 90.0) -> list[CheckResult]:
    return list(await asyncio.gather(*(check_model(cfg, s, timeout=timeout) for s in (specs or default_specs(cfg)))))


def format_report(results: list[CheckResult], *, color: bool = True) -> str:
    def c(code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    if not results:
        return "没有可检查的模型：请在环境变量或 .env 中配置至少一个供应商的密钥（如 DEEPSEEK_API_KEY）。"
    lines = []
    for r in results:
        mark = c("32", "✓") if r.ok else c("31", "✗")
        lines.append(f"{mark} {r.spec}  {c('2', r.label)}")
        if r.error:
            lines.append(f"    {c('31', r.error[:300])}")
        for s in r.steps:
            lines.append(f"    {'✓' if s.ok else '✗'} {s.name:<6} {s.seconds:5.1f}s  {s.detail}")
        if r.steps:
            served = f"实际模型 {r.served_model}  " if r.served_model and not r.spec.endswith(r.served_model) else ""
            lines.append(c("2", f"    {served}思考 {r.thinking_chars} 字 · 输入 {r.input_tokens} / 输出 {r.output_tokens} tokens"
                                f" · 缓存命中 {r.cache_read_tokens} · ${r.cost_usd:.6f}"))
    ok = sum(r.ok for r in results)
    lines.append(f"\n{ok}/{len(results)} 个模型通过")
    return "\n".join(lines)


__all__ = ["CheckResult", "Step", "check_model", "check_all", "default_specs", "format_report"]
