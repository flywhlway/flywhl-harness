"""模型接入层。``create_provider`` 根据配置/环境变量选择供应商与模型，并统一包上重试与降级。"""

from __future__ import annotations

from ..config import HarnessConfig
from ..runtime.retry import RetryPolicy
from .base import ErrorKind, MessageBuilder, ModelRequest, Provider, ProviderError, StreamEvent, prepare_history
from .mock import DemoProvider, ScriptedProvider
from .models import MODELS, ModelInfo, compute_cost, get_model_info
from .profiles import DEFAULT_MODEL, PROFILES, ProviderProfile, detect_profile, resolve, split_spec
from .replay import ReplayProvider, context_fingerprint
from .resilient import ResilientProvider


def detect_provider() -> str:
    """auto 模式：按 ``AUTO_ORDER`` 找第一个配置了密钥的供应商；都没有就用内置演示模型，保证开箱即用。"""
    return detect_profile().name


def _make(profile: ProviderProfile, cfg: HarnessConfig, *, own_endpoint: bool) -> Provider:
    """``own_endpoint``：配置里的 base_url / api_key 只属于主模型的供应商，降级到别家时用各自档案的默认值。"""
    base = cfg.base_url if own_endpoint else None
    key = (cfg.api_key or (profile.api_key(cfg.api_key_env) if cfg.api_key_env else None)) if own_endpoint else None
    if profile.kind == "mock":
        return DemoProvider()
    if profile.kind == "anthropic":
        from .anthropic import AnthropicProvider

        return AnthropicProvider(api_key=key, base_url=base, timeout=cfg.request_timeout,
                                 effort=cfg.reasoning_effort, thinking=cfg.thinking != "disabled")
    from .openai_compat import OpenAICompatProvider

    return OpenAICompatProvider(api_key=key, base_url=base, timeout=cfg.request_timeout, profile=profile,
                                thinking=cfg.thinking, reasoning_effort=cfg.reasoning_effort,
                                extra_body=cfg.extra_body if own_endpoint else None)


def create_provider(cfg: HarnessConfig) -> tuple[Provider, str]:
    """返回 (provider, model)。真实 provider 会被 ResilientProvider 包装：重试 + 熔断 + 降级链。

    模型写法 ``<供应商>:<模型>``；降级链里的每一项也可以是别家的模型（跨供应商降级）。
    """
    profile, model = resolve(cfg.model, cfg.provider)
    primary = _make(profile, cfg, own_endpoint=True)
    if profile.kind == "mock":
        return primary, model
    made: dict[str, Provider] = {profile.name: primary}
    chain: list[tuple[Provider, str]] = [(primary, model)]
    for spec in cfg.fallback_models:
        fp, fm = resolve(spec, profile.name)
        if fp.kind == "mock":
            continue
        if fp.name not in made:
            made[fp.name] = _make(fp, cfg, own_endpoint=False)
        chain.append((made[fp.name], fm))
    return ResilientProvider(chain, RetryPolicy(max_attempts=cfg.max_retries)), model


__all__ = [
    "Provider", "ModelRequest", "StreamEvent", "ProviderError", "ErrorKind", "MessageBuilder", "prepare_history",
    "ScriptedProvider", "DemoProvider", "ReplayProvider", "ResilientProvider", "ModelInfo", "MODELS",
    "get_model_info", "compute_cost", "context_fingerprint", "create_provider", "detect_provider",
    "PROFILES", "ProviderProfile", "DEFAULT_MODEL", "resolve", "split_spec",
]
