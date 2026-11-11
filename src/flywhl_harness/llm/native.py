"""模型侧联网（供应商自带的搜索）：按供应商声明能力，运行时按声明决定开不开、怎么计数、要不要开局置污点。

和内置联网工具（``web_search`` / ``web_fetch``）的区别：搜索在模型供应商那里发生，结果直接进模型上下文，harness 只能
事后从响应里读到计数和来源，没法在结果进上下文之前过滤或标注。所以：

- 只有声明里写明“已确认”的模型才启用；不能与函数工具同时使用的不启用（harness 的请求总带函数工具）；
- 看得到搜索的（``observable``），第一次出现搜索结果时置污点；看不到的，会话开局即置污点；
- 计数进 ``Budget.web_searches``，按声明里的单价计入费用上限；看不到计数的不计数，设置页明确说明；
- 会话在建立时二选一：用联网工具，或用模型侧联网，之后只能收紧（关闭）；
- 降级到别家模型时，声明对不上，请求里不再带联网参数，时间线发一条 notice。

``request`` 的含义随协议不同：Anthropic 协议是追加到 ``tools`` 的 server tool；OpenAI 兼容协议是并入请求体的字段。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

ANTHROPIC_HOST = "api.anthropic.com"
_SERVER_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}


@dataclass(frozen=True)
class NativeSearchSpec:
    name: str                          # 声明的键，记进会话能力位与 assistant 消息
    label: str                         # 给人看的名字
    provider: str                      # 供应商档案名（anthropic、dashscope ……）
    hosts: tuple[str, ...]             # 端点主机：同一协议经不同端点能力不同（DeepSeek 的 Anthropic 兼容端点）
    request: dict[str, Any]
    models: tuple[str, ...]            # 已确认可用的模型（前缀匹配）；不在列表里就不启用
    with_tools: bool                   # 已确认能与函数工具同时使用
    usage_counts: tuple[str, ...] = ()  # usage.server_tool_use 下的计数字段，取最大值（同一次搜索可能在两个字段都记）
    citations: bool = False            # 响应里有逐条来源
    observable: bool = False           # 看得到“发生了搜索”：看不到就开局置污点
    price_per_call: float = 0.0        # 美元 / 次
    notes: str = ""                    # 未核实的地方
    region: str = ""


NATIVE_SPECS: dict[str, NativeSearchSpec] = {s.name: s for s in (
    NativeSearchSpec(
        "anthropic", "Claude 联网搜索（Anthropic 官方 API）", "anthropic", (ANTHROPIC_HOST,), {"tools": [_SERVER_TOOL]},
        models=("claude-",), with_tools=True, usage_counts=("web_search_requests",), citations=True, observable=True,
        price_per_call=0.01, region="境外"),
    NativeSearchSpec(
        "deepseek-anthropic", "DeepSeek（Anthropic 兼容端点）", "anthropic", ("api.deepseek.com",), {"tools": [_SERVER_TOOL]},
        models=("deepseek-v4-pro",), with_tools=False, usage_counts=("web_search_requests", "tool_search"),
        observable=True, price_per_call=0.00075, region="境内",
        notes="官方示例只有 deepseek-v4-pro 且关闭思考、不带函数工具；deepseek-flash 与函数工具同时使用未确认"),
    NativeSearchSpec(
        "dashscope", "百炼 enable_search", "dashscope", ("dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"),
        {"enable_search": True, "search_options": {"search_strategy": "turbo"}}, models=("qwen",), with_tools=False,
        price_per_call=0.00042, region="境内",
        notes="OpenAI 兼容方式下响应里没有来源；与函数工具同时使用未确认；超过 15 RPS 时不报错而是跳过搜索"),
)}


def _target(provider: Any) -> tuple[Any, str | None]:
    """降级链取主模型那一家。"""
    chain = getattr(provider, "chain", None)
    if chain:
        return chain[0][0], chain[0][1]
    return provider, None


def provider_host(provider: Any) -> str:
    base = getattr(provider, "base_url", None)
    if getattr(provider, "name", "") == "anthropic" and not base:
        return ANTHROPIC_HOST
    return (urlsplit(base).hostname or "").lower() if base else ""


def matches(spec: NativeSearchSpec, provider: Any, model: str) -> bool:
    """这家 provider 加这个模型是不是这份声明说的那一家：provider 自己发请求前也要核对，降级到别家时不带联网参数。"""
    return (getattr(provider, "name", "") == spec.provider and provider_host(provider) in spec.hosts
            and model.startswith(spec.models))


def native_for(provider: Any, model: str) -> tuple[NativeSearchSpec | None, str]:
    """(可用的声明, 原因)。没有声明的供应商返回 (None, 原因)；有声明但没确认的也返回 None，原因说明缺什么。"""
    p, chain_model = _target(provider)
    model = chain_model or model
    name, host = getattr(p, "name", ""), provider_host(p)
    cands = [s for s in NATIVE_SPECS.values() if s.provider == name and host in s.hosts]
    if not cands:
        return None, f"当前主模型的供应商（{name or '未知'}）没有模型侧联网的声明"
    spec = cands[0]
    if not model.startswith(spec.models):
        return None, f"{spec.label} 只在已确认的模型上启用（{'、'.join(spec.models)}），当前是 {model}"
    if not spec.with_tools:
        return None, f"{spec.label} 未确认能与函数工具同时使用" + (f"（{spec.notes}）" if spec.notes else "")
    return spec, ""


def usage_count(spec: NativeSearchSpec, server_tool_use: dict[str, Any] | None) -> int | None:
    if not server_tool_use or not spec.usage_counts:
        return None
    vals = [int(v) for k in spec.usage_counts if isinstance(v := server_tool_use.get(k), (int, float))]
    return max(vals) if vals else None


def server_tools(spec: NativeSearchSpec, opts: dict[str, Any]) -> list[dict[str, Any]]:
    """Anthropic 协议的 server tool 定义，套上本次请求的限制：``max_uses`` 取声明与剩余搜索次数的较小值，
    ``deny_domains`` 映射为 ``blocked_domains``（同样含子域名）。"""
    out = []
    for t in spec.request.get("tools", ()):
        t = dict(t)
        if isinstance(n := opts.get("max_uses"), int) and n > 0:
            t["max_uses"] = min(int(t.get("max_uses") or n), n)
        if deny := [d.strip().lower().lstrip(".") for d in opts.get("deny_domains") or () if d.strip()]:
            t["blocked_domains"] = deny
        out.append(t)
    return out


def summarize(spec: NativeSearchSpec, blocks: list[dict[str, Any]], server_tool_use: dict[str, Any] | None,
              cited: list[str]) -> dict[str, Any] | None:
    """从一次响应的供应商侧块里整理出给时间线和计数用的摘要；没发生搜索时返回 None。

    次数以 usage 为准（供应商按它计费）；usage 里没有时按 ``server_tool_use`` 块数。来源按网址去重编号。"""
    queries, sources, errors, seen = [], [], [], set()
    for d in blocks:
        kind = d.get("type")
        if kind == "server_tool_use" and d.get("name") == "web_search":
            q = (d.get("input") or {}).get("query")
            queries.append(q if isinstance(q, str) else "")
        elif kind == "web_search_tool_result":
            content = d.get("content")
            if isinstance(content, dict):   # 搜索失败时 content 是错误对象
                errors.append(str(content.get("error_code") or "unknown"))
                continue
            for r in content if isinstance(content, list) else ():
                url = r.get("url") if isinstance(r, dict) else None
                if isinstance(url, str) and url and url not in seen:
                    seen.add(url)
                    sources.append({"n": len(sources) + 1, "title": str(r.get("title") or ""), "url": url,
                                    "page_age": r.get("page_age"), "cited": url in cited})
    count = usage_count(spec, server_tool_use)
    if count is None:
        count = len(queries)
    if not count and not sources and not errors:
        return None
    return {"spec": spec.name, "searches": count, "queries": [q for q in queries if q], "sources": sources,
            "errors": errors}


__all__ = ["NATIVE_SPECS", "NativeSearchSpec", "matches", "native_for", "provider_host", "server_tools", "summarize",
           "usage_count"]
