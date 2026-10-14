"""模型能力矩阵与价格表。

harness 需要知道每个模型的：上下文窗口（决定何时压缩）、最大输出、价格（计算成本、执行费用预算），
以及若干行为特征（是否要求历史严格只追加等）。

价格单位：美元 / 百万 token，**以各家官方价格页为准**（DeepSeek 价格核对于 2026-10，见其 Models & Pricing 页）。
未登记的模型使用保守默认值（128K 窗口、成本记 0），可通过配置覆盖窗口大小；
OpenRouter 这类在 usage 里直接返回实际费用的服务，以返回值为准。
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
_DS_FLASH = dict(context_window=1_000_000, max_output=384_000, input_price=0.30, output_price=1.20,
                 cache_read_price=0.006, supports_thinking=True)

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
    # ---- DeepSeek（OpenAI 兼容；价格取高峰时段价作为上限，非高峰时段为其一半，以官方价格页为准）----
    # deepseek-flash 即 DeepSeek-V4.1-Flash：默认开启思考模式，带 tools 的多轮请求必须回传 reasoning_content
    "deepseek-flash": ModelInfo("deepseek-flash", "deepseek", **_DS_FLASH),
    "deepseek-v4-pro": ModelInfo("deepseek-v4-pro", "deepseek", context_window=1_000_000, max_output=384_000,
                                 input_price=1.32, output_price=3.96, cache_read_price=0.044, supports_thinking=True),
    # 旧模型名仍被接受，实际由 deepseek-flash 提供服务并按其价格计费
    "deepseek-chat": ModelInfo("deepseek-chat", "deepseek", **_DS_FLASH),
    "deepseek-reasoner": ModelInfo("deepseek-reasoner", "deepseek", **_DS_FLASH),
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
