"""供应商档案：把“某家服务怎么接”收敛成一份数据，而不是散落在各处的 if/else。

模型统一写成 ``<供应商>:<模型>``，例如 ``deepseek:deepseek-flash``、``dashscope:qwen3.8-flash``、
``ollama:qwen3.5:9b-mlx``（只按第一个冒号切分，且前缀必须是已知供应商）。只写模型名时：
模型能力表里登记过的按表里的供应商，否则沿用 ``provider`` 配置。

各家 OpenAI 兼容接口在“思考内容”上的差异（真实接口实测，录制的流见 ``tests/fixtures/sse/``）：

============  ==========================  ===================================  ==========================
供应商         思考内容从哪来                多轮如何回传                           思考开关
============  ==========================  ===================================  ==========================
deepseek      ``delta.reasoning_content``  带 tools 时必须回传 reasoning_content   ``thinking.type``
kimi          ``delta.reasoning_content``  回传 reasoning_content                  ``thinking.type``
dashscope     ``delta.reasoning_content``  不需要回传                              ``enable_thinking``
zhipu         ``delta.reasoning_content``  不需要回传                              ``thinking.type``
minimax       正文内联 ``<think>…</think>``  整段正文（含 think）原样回传             —
openrouter    ``delta.reasoning``          不回传（只展示）                        ``reasoning.enabled``
============  ==========================  ===================================  ==========================
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    label: str
    kind: str = "openai"                 # openai（Chat Completions 兼容）| anthropic | mock
    base_url: str | None = None
    base_url_env: str | None = None      # 允许用环境变量改写 base_url（本地服务、代理）
    key_env: tuple[str, ...] = ()        # 依次查找的密钥环境变量
    default_model: str = ""
    needs_key: bool = True
    reasoning_echo: str = "none"         # none | field（reasoning_content 字段）| inline（<think> 标签内联到正文）
    think_tags: bool = False             # 正文开头可能内联 <think>…</think>，流式拆成思考与正文
    thinking_param: str | None = None    # type → {"thinking": {"type": ...}} | enable → {"enable_thinking": bool} | reasoning
    effort_param: str | None = None      # reasoning_effort → 顶层字段 | reasoning → {"reasoning": {"effort": ...}}
    reports_cost: bool = False           # usage 里直接带实际费用（OpenRouter），比按价格表估算准确

    def api_key(self, env_name: str | None = None) -> str | None:
        for k in ((env_name,) if env_name else self.key_env):
            if k and os.environ.get(k):
                return os.environ[k]
        return None

    def resolved_base_url(self) -> str | None:
        return (os.environ.get(self.base_url_env) if self.base_url_env else None) or self.base_url

    def available(self) -> bool:
        return not self.needs_key or bool(self.api_key())

    def request_options(self, thinking: str | None, effort: str | None) -> dict[str, Any]:
        """把通用的“思考开关 / 思考强度”翻译成这家服务的请求字段；不支持的直接忽略。"""
        body: dict[str, Any] = {}
        if thinking in ("enabled", "disabled"):
            on = thinking == "enabled"
            if self.thinking_param == "type":
                body["thinking"] = {"type": thinking}
            elif self.thinking_param == "enable":
                body["enable_thinking"] = on
            elif self.thinking_param == "reasoning":
                body["reasoning"] = {"enabled": on}
        if effort and thinking != "disabled":
            if self.effort_param == "reasoning_effort":
                body["reasoning_effort"] = effort
            elif self.effort_param == "reasoning":
                body.setdefault("reasoning", {})["effort"] = effort
        return body


PROFILES: dict[str, ProviderProfile] = {p.name: p for p in [
    ProviderProfile("deepseek", "DeepSeek", base_url="https://api.deepseek.com", key_env=("DEEPSEEK_API_KEY",),
                    default_model="deepseek-flash", reasoning_echo="field", thinking_param="type",
                    effort_param="reasoning_effort"),
    ProviderProfile("dashscope", "阿里云百炼（通义千问）", base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                    key_env=("DASHSCOPE_API_KEY",), default_model="qwen3.8-flash", thinking_param="enable"),
    ProviderProfile("kimi", "Moonshot（Kimi）", base_url="https://api.moonshot.cn/v1",
                    key_env=("KIMI_API_KEY", "MOONSHOT_API_KEY"), default_model="kimi-k2.6",
                    reasoning_echo="field", thinking_param="type"),
    ProviderProfile("zhipu", "智谱（GLM）", base_url="https://open.bigmodel.cn/api/paas/v4", key_env=("ZHIPU_API_KEY",),
                    default_model="glm-5.3-flash", thinking_param="type"),
    ProviderProfile("minimax", "MiniMax", base_url="https://api.minimaxi.com/v1", key_env=("MINIMAX_API_KEY",),
                    default_model="MiniMax-M2.7", reasoning_echo="inline", think_tags=True),
    ProviderProfile("openrouter", "OpenRouter", base_url="https://openrouter.ai/api/v1", key_env=("OPENROUTER_API_KEY",),
                    default_model="deepseek/deepseek-v4.1-flash", thinking_param="reasoning", effort_param="reasoning",
                    reports_cost=True),
    ProviderProfile("openai", "OpenAI 或任意兼容服务", base_url="https://api.openai.com/v1", base_url_env="OPENAI_BASE_URL",
                    key_env=("OPENAI_API_KEY",), default_model="gpt-5.4-mini", effort_param="reasoning_effort"),
    ProviderProfile("ollama", "本地 Ollama", base_url="http://localhost:11434/v1", base_url_env="OLLAMA_BASE_URL",
                    default_model="qwen3.5:9b-mlx", needs_key=False, think_tags=True),
    ProviderProfile("anthropic", "Anthropic（Claude）", kind="anthropic", base_url_env="ANTHROPIC_BASE_URL",
                    key_env=("ANTHROPIC_API_KEY",), default_model="claude-opus-5-5"),
    ProviderProfile("mock", "内置演示模型", kind="mock", default_model="flywhl-demo", needs_key=False),
]}

# auto 模式下按这个顺序找第一个配置了密钥的供应商；都没有就用内置演示模型（开箱即用）
AUTO_ORDER = ("deepseek", "anthropic", "openai", "dashscope", "kimi", "zhipu", "minimax", "openrouter")
DEFAULT_MODEL = "deepseek:deepseek-flash"   # 项目推荐的默认模型（有 DEEPSEEK_API_KEY 时 auto 即选它）


def split_spec(spec: str) -> tuple[str | None, str]:
    """``deepseek:deepseek-flash`` → ("deepseek", "deepseek-flash")；前缀不是已知供应商时整体视为模型名。"""
    head, sep, rest = spec.partition(":")
    if sep and head in PROFILES and rest:
        return head, rest
    return None, spec


def detect_profile() -> ProviderProfile:
    for name in AUTO_ORDER:
        p = PROFILES[name]
        if p.api_key():
            return p
    return PROFILES["mock"]


def resolve(spec: str | None, provider: str = "auto") -> tuple[ProviderProfile, str]:
    """解析 (供应商档案, 模型名)。优先级：spec 里的前缀 > 显式 provider > 模型表登记的供应商 > auto 探测。"""
    from .models import MODELS

    prefix, model = split_spec(spec) if spec else (None, "")
    if prefix:
        return PROFILES[prefix], model
    if provider != "auto":
        if provider not in PROFILES:
            raise ValueError(f"未知 provider：{provider}（可选：auto、{'、'.join(PROFILES)}）")
        p = PROFILES[provider]
        # 兼容旧配置：provider=openai 但只配了 DEEPSEEK_API_KEY → 走 DeepSeek
        if p.name == "openai" and not p.api_key() and not os.environ.get("OPENAI_BASE_URL") and PROFILES["deepseek"].api_key():
            p = PROFILES["deepseek"]
        return p, model or p.default_model
    if model and model in MODELS and MODELS[model].provider in PROFILES:
        return PROFILES[MODELS[model].provider], model
    p = detect_profile()
    return p, model or p.default_model


__all__ = ["ProviderProfile", "PROFILES", "AUTO_ORDER", "DEFAULT_MODEL", "split_spec", "detect_profile", "resolve"]
