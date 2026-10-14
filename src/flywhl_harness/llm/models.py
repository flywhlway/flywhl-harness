"""模型能力矩阵与价格表。

harness 需要知道每个模型的：上下文窗口（决定何时压缩）、最大输出、价格（计算成本、执行费用预算），
以及若干行为特征（是否要求历史严格只追加等）。

价格单位：美元 / 百万 token。表中价格仅作示例，**以各家官方价格页为准**；
未知模型使用保守默认值（128K 窗口、成本记 0），可通过配置覆盖窗口大小。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..types import Usage


@dataclass(frozen=True)
class ModelInfo:
    id: str
    provider: str
    context_window: int = 128_000
    max_output: int = 8_192
    input_price: float = 0.0
    output_price: float = 0.0
    cache_read_price: float = 0.0
    cache_write_price: float = 0.0
    supports_tools: bool = True
    supports_thinking: bool = False
    # 历史必须严格只追加：编辑早先轮次会让 thinking 签名失效（Claude 新模型）并打碎前缀缓存。
    # 为 True 时 harness 不做“客户端清理旧工具结果”这类改写历史的优化。
    append_only: bool = False
    # 是否支持 server-side refusal fallback（Anthropic 的 fallbacks 参数）
    server_fallback: bool = False


_CLAUDE = dict(provider="anthropic", context_window=1_000_000, max_output=128_000, supports_thinking=True, append_only=True)

MODELS: dict[str, ModelInfo] = {
    # ---- Anthropic（价格：输入 / 输出 / 缓存读 / 缓存写(5 分钟)） ----
    "claude-opus-5-5": ModelInfo("claude-opus-5-5", input_price=4.0, output_price=20.0, cache_read_price=0.20,
                                 cache_write_price=5.0, server_fallback=True, **_CLAUDE),
    "claude-sonnet-5-5": ModelInfo("claude-sonnet-5-5", input_price=2.0, output_price=10.0, cache_read_price=0.20,
                                   cache_write_price=2.5, server_fallback=True, **_CLAUDE),
    "claude-haiku-5-5": ModelInfo("claude-haiku-5-5", input_price=0.10, output_price=0.50, cache_read_price=0.01,
                                  cache_write_price=0.125, **_CLAUDE),
    "claude-fable-5-1": ModelInfo("claude-fable-5-1", input_price=10.0, output_price=50.0, cache_read_price=0.25,
                                  cache_write_price=12.5, server_fallback=True, **_CLAUDE),
    # ---- OpenAI 兼容（示例：DeepSeek / 通义 / 本地 vLLM、Ollama）----
    "deepseek-chat": ModelInfo("deepseek-chat", "openai", context_window=128_000, max_output=8_192,
                               input_price=0.28, output_price=0.42, cache_read_price=0.028),
    "deepseek-reasoner": ModelInfo("deepseek-reasoner", "openai", context_window=128_000, max_output=32_768,
                                   input_price=0.28, output_price=0.42, cache_read_price=0.028, supports_thinking=True),
    # ---- 内置演示模型：窗口刻意设小，方便观察上下文压缩 ----
    "flywhl-demo": ModelInfo("flywhl-demo", "mock", context_window=24_000, max_output=4_096),
    "scripted": ModelInfo("scripted", "mock", context_window=200_000, max_output=8_192),
}


def get_model_info(model: str, provider: str = "openai", context_window: int | None = None) -> ModelInfo:
    info = MODELS.get(model) or ModelInfo(model, provider)
    if context_window:
        info = replace(info, context_window=context_window)
    return info


def compute_cost(info: ModelInfo, usage: Usage) -> float:
    """按百万 token 单价计算一次调用的成本（美元）。"""
    m = 1_000_000
    return round(
        usage.input_tokens * info.input_price / m
        + usage.output_tokens * info.output_price / m
        + usage.cache_read_tokens * info.cache_read_price / m
        + usage.cache_write_tokens * info.cache_write_price / m,
        8,
    )
