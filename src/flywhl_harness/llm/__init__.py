"""模型接入层。``create_provider`` 根据配置/环境变量选择 provider，并统一包上重试与降级。"""

from __future__ import annotations

import os

from ..config import HarnessConfig
from ..runtime.retry import RetryPolicy
from .base import ErrorKind, MessageBuilder, ModelRequest, Provider, ProviderError, StreamEvent, prepare_history
from .mock import DemoProvider, ScriptedProvider
from .models import MODELS, ModelInfo, compute_cost, get_model_info
from .replay import ReplayProvider, context_fingerprint
from .resilient import ResilientProvider

DEFAULT_MODELS = {"anthropic": "claude-opus-5-5", "openai": "deepseek-chat", "mock": "flywhl-demo"}


def detect_provider() -> str:
    """auto 模式：有哪个 Key 用哪个；都没有就用内置演示模型，保证开箱即用。"""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY"):
        return "openai"
    return "mock"


def _make(kind: str, cfg: HarnessConfig) -> Provider:
    if kind == "mock":
        return DemoProvider()
    if kind == "openai":
        from .openai_compat import OpenAICompatProvider

        base = cfg.base_url or os.environ.get("OPENAI_BASE_URL")
        if not base and os.environ.get("DEEPSEEK_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
            base = "https://api.deepseek.com"
        return OpenAICompatProvider(api_key=cfg.api_key, base_url=base, timeout=cfg.request_timeout)
    if kind == "anthropic":
        from .anthropic import AnthropicProvider

        return AnthropicProvider(api_key=cfg.api_key, base_url=cfg.base_url, timeout=cfg.request_timeout)
    raise ValueError(f"未知 provider：{kind}（可选 auto / mock / openai / anthropic）")


def create_provider(cfg: HarnessConfig) -> tuple[Provider, str]:
    """返回 (provider, model)。真实 provider 会被 ResilientProvider 包装：重试 + 熔断 + 降级链。"""
    kind = detect_provider() if cfg.provider == "auto" else cfg.provider
    model = cfg.model or DEFAULT_MODELS.get(kind, "flywhl-demo")
    base = _make(kind, cfg)
    if kind == "mock":
        return base, model
    chain = [(base, model)] + [(base, m) for m in cfg.fallback_models]
    return ResilientProvider(chain, RetryPolicy(max_attempts=cfg.max_retries)), model


__all__ = [
    "Provider", "ModelRequest", "StreamEvent", "ProviderError", "ErrorKind", "MessageBuilder", "prepare_history",
    "ScriptedProvider", "DemoProvider", "ReplayProvider", "ResilientProvider", "ModelInfo", "MODELS",
    "get_model_info", "compute_cost", "context_fingerprint", "create_provider", "detect_provider",
]
