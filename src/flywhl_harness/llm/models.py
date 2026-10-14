"""模型能力矩阵与价格表。

harness 需要知道每个模型的：上下文窗口（决定何时压缩）、最大输出、价格（计算成本、执行费用预算），
以及若干行为特征（是否要求历史严格只追加等）。

价格单位：每百万 token，币种按各家官方价格页（``currency``）：Claude 与 DeepSeek 为美元，百炼、Kimi、MiniMax 为人民币。
``compute_cost()`` 统一折算成美元（预算 ``max_cost_usd`` 与界面都用美元），人民币按 ``CNY_PER_USD`` 折算，
可用环境变量 ``FLYWHL_CNY_PER_USD`` 覆盖。所有价格核对于 2026-10-14，**以各家官方价格页为准**。

未登记的模型使用保守默认值（128K 窗口、成本记 0），可通过配置覆盖窗口大小；
OpenRouter 这类在 usage 里直接返回实际费用的服务，以返回值为准。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace

from ..types import Usage

CNY_PER_USD = 6.70   # 2026-10-14 的美元兑人民币汇率；只影响人民币计价模型折算成美元的结果


@dataclass(frozen=True)
class PriceTier:
    """长上下文加价：单次请求的输入侧 token（含缓存）超过 ``above`` 时，整次请求改按这一档计价。"""

    above: int
    input_price: float
    output_price: float
    cache_read_price: float = 0.0
    cache_write_price: float = 0.0


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
    currency: str = "USD"                 # 价格的币种（以官方价格页为准）：USD | CNY
    tiers: tuple[PriceTier, ...] = ()     # 按输入规模分档的价格，按 above 升序


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
    # ---- 阿里云百炼（人民币，华北2 北京地域）：0–1M 输入单一档；隐式缓存命中按输入单价的 20% 计费 ----
    "qwen3.8-flash": ModelInfo("qwen3.8-flash", "dashscope", context_window=1_000_000, input_price=0.8, output_price=2.7,
                               cache_read_price=0.16, supports_thinking=True, currency="CNY"),
    "qwen3.8-max": ModelInfo("qwen3.8-max", "dashscope", context_window=1_000_000, input_price=12.0, output_price=36.0,
                             cache_read_price=2.4, supports_thinking=True, currency="CNY"),
    # ---- Moonshot Kimi（人民币）：K2 系列无缓存写入费；K3 缓存写入按 5 分钟档 ----
    "kimi-k2.6": ModelInfo("kimi-k2.6", "kimi", context_window=262_144, input_price=6.5, output_price=27.0,
                           cache_read_price=1.1, supports_thinking=True, currency="CNY"),
    "kimi-k2.7-code": ModelInfo("kimi-k2.7-code", "kimi", context_window=262_144, input_price=6.5, output_price=27.0,
                                cache_read_price=1.3, supports_thinking=True, currency="CNY"),
    "kimi-k2.7-code-highspeed": ModelInfo("kimi-k2.7-code-highspeed", "kimi", context_window=262_144, input_price=13.0,
                                          output_price=54.0, cache_read_price=2.6, supports_thinking=True, currency="CNY"),
    "kimi-k3": ModelInfo("kimi-k3", "kimi", context_window=1_048_576, input_price=20.0, output_price=100.0,
                         cache_read_price=2.0, cache_write_price=20.0, supports_thinking=True, currency="CNY"),
    # ---- MiniMax（人民币，标准服务）：M3 为“永久五折”后的价格，输入超过 512K 整次请求按高一档计价 ----
    "MiniMax-M2.7": ModelInfo("MiniMax-M2.7", "minimax", context_window=204_800, input_price=2.1, output_price=8.4,
                              cache_read_price=0.42, cache_write_price=2.625, supports_thinking=True, currency="CNY"),
    "MiniMax-M2.7-highspeed": ModelInfo("MiniMax-M2.7-highspeed", "minimax", context_window=204_800, input_price=4.2,
                                        output_price=16.8, cache_read_price=0.42, cache_write_price=2.625,
                                        supports_thinking=True, currency="CNY"),
    "MiniMax-M3": ModelInfo("MiniMax-M3", "minimax", context_window=1_000_000, input_price=2.1, output_price=8.4,
                            cache_read_price=0.42, supports_thinking=True, currency="CNY",
                            tiers=(PriceTier(512_000, 4.2, 16.8, 0.84),)),
    # ---- 内置演示模型：窗口刻意设小，方便观察上下文压缩 ----
    "flywhl-demo": ModelInfo("flywhl-demo", "mock", context_window=24_000, max_output=4_096),
    "scripted": ModelInfo("scripted", "mock", context_window=200_000, max_output=8_192),
}


def get_model_info(model: str, provider: str = "openai", context_window: int | None = None) -> ModelInfo:
    info = MODELS.get(model) or ModelInfo(model, provider)
    if context_window:
        info = replace(info, context_window=context_window)
    return info


def usd_per_unit(currency: str) -> float:
    """1 单位该币种折合多少美元。"""
    if currency == "USD":
        return 1.0
    if currency == "CNY":
        return 1.0 / float(os.environ.get("FLYWHL_CNY_PER_USD") or CNY_PER_USD)
    raise ValueError(f"未知币种：{currency}")


def compute_cost(info: ModelInfo, usage: Usage) -> float:
    """按百万 token 单价计算一次调用的成本，统一折算成美元。分档价格按本次请求的输入规模选档。"""
    price: ModelInfo | PriceTier = info
    for tier in info.tiers:
        if usage.context_tokens > tier.above:
            price = tier
    m = 1_000_000
    native = (
        usage.input_tokens * price.input_price / m
        + usage.output_tokens * price.output_price / m
        + usage.cache_read_tokens * price.cache_read_price / m
        + usage.cache_write_tokens * price.cache_write_price / m
    )
    return round(native * usd_per_unit(info.currency), 8)
