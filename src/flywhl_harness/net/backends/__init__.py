"""搜索 / 读取后端。每家一个模块，只做请求构造、响应映射和错误分类。"""

from __future__ import annotations

from typing import Mapping

from ...config import WebConfig
from ..search import FetchBackend, SearchBackend


def _opts(cfg: WebConfig, name: str) -> dict:
    return dict(cfg.backend.get(name) or {})


def make_backend(name: str, cfg: WebConfig, env: Mapping[str, str]) -> SearchBackend:
    opts = _opts(cfg, name)
    if name == "deepseek":
        from .deepseek import DeepSeekSearch

        return DeepSeekSearch(opts, env)
    if name == "bocha":
        from .bocha import BochaSearch

        return BochaSearch(opts, env)
    if name == "searxng":
        from .searxng import SearxngSearch

        return SearxngSearch(opts)
    raise ValueError(f"未知的搜索后端：{name}")   # 配置校验已经挡住，这里是保险


def make_fetcher(cfg: WebConfig, env: Mapping[str, str]) -> FetchBackend | None:
    if cfg.fetch_backend == "deepseek":
        from .deepseek import DeepSeekFetch

        return DeepSeekFetch(_opts(cfg, "deepseek"), env)
    if cfg.fetch_backend == "direct":
        from .direct import DirectFetch

        return DirectFetch()
    return None


__all__ = ["make_backend", "make_fetcher"]
