"""Anthropic Messages API provider（基于官方 ``anthropic`` Python SDK 的原始流事件）。

要点
----
1. **SDK 重试设为 0**：重试统一由 ``ResilientProvider`` 负责（全链路只允许一层重试）。
2. **原始事件归一化**：``message_start → content_block_start/delta/stop → message_delta → message_stop``
   映射为 harness 的 StreamEvent；工具参数以 ``input_json_delta`` 增量到达。
3. **tool_result 合并**：同一轮的所有 tool_result 必须放在 **同一条** user 消息里回传
   （拆成多条会让模型逐渐不再并行调用工具），且排在任何文本块之前。
4. **thinking 原样回传**：带签名的 thinking 块要原样回传；历史必须 **只追加**——编辑早先轮次会让
   之后所有 thinking 块失效（新模型会直接 400），也会打碎前缀缓存。
5. **前缀缓存**：顶层 ``cache_control`` 自动缓存最后一个可缓存块；system 与 tools 在会话内冻结。
6. **refusal 与 server-side fallback**：先检查 ``stop_reason == "refusal"`` 再读内容；对支持的模型默认
   开启 ``fallbacks: "default"``（beta ``server-side-fallback-2026-07-01``），被安全策略拒答时由服务端
   在同一次调用里改用推荐的备用模型。流中出现 ``fallback`` 块时，之前的 thinking/tool_use 不再回传。
   该参数只在官方端点上发送：经 OpenRouter 等兼容网关调用 Claude 时自动关闭（网关会 400）。
7. **模型侧联网**：请求带了 ``native_search`` 且声明与本端点对得上时，追加 ``web_search`` server tool。
   响应里的 ``server_tool_use`` / ``web_search_tool_result`` 存为 ``ServerBlock`` 原样回传（只回同一端点、
   且调用与结果成对时才回传），搜索次数、来源与被引用的网址整理进 ``meta["native_web"]``。
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator

from ..cancel import CancelToken
from ..types import Message, ServerBlock, StopReason, ThinkingBlock, ToolCall, Usage
from .base import (
    ErrorKind, MessageBuilder, ModelRequest, Provider, ProviderError, StreamEvent, classify_http_error, prepare_history,
)
from .models import compute_cost
from .native import NATIVE_SPECS, matches, provider_host, server_tools, summarize

_STOP = {
    "end_turn": StopReason.END_TURN,
    "stop_sequence": StopReason.END_TURN,
    "pause_turn": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "refusal": StopReason.REFUSAL,
}
_REDACTED = "redacted:"


def _plain(obj: Any) -> Any:
    """SDK 对象 → 纯 JSON 结构（供应商侧块要原样回传，存储也要能序列化）。"""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dump(mode="json", exclude_none=True)
    return {k: _plain(v) for k, v in vars(obj).items() if v is not None and not k.startswith("_")}


def _server_echo(m: Message, host: str) -> set[str]:
    """这条 assistant 消息里可以回传的供应商侧块的调用 id：同一端点产生、调用与结果成对。

    只有调用没有结果（截断）或只有结果的块回传会 400；别的端点不认识这些块，也不回传。"""
    if m.provider != "anthropic" or not host:
        return set()
    blocks = [c.data for c in m.content if isinstance(c, ServerBlock) and c.origin == host]
    used = {d.get("id") for d in blocks if d.get("type") == "server_tool_use"}
    answered = {d.get("tool_use_id") for d in blocks if d.get("type") != "server_tool_use"}
    return {i for i in used & answered if isinstance(i, str)}


def to_anthropic_messages(messages: list[Message], host: str = "") -> list[dict[str, Any]]:
    """统一消息 → Anthropic 消息：tool 结果并入 user 消息；同角色相邻消息合并。

    ``host`` 是要发往的端点主机，供应商侧块只回传给产生它的同一端点。"""
    out: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            prev = out[-1]["content"]
            if role == "user" and blocks[0]["type"] == "tool_result":
                # tool_result 必须排在同一条 user 消息的文本之前
                texts = [b for b in prev if b["type"] != "tool_result"]
                results = [b for b in prev if b["type"] == "tool_result"]
                out[-1]["content"] = results + blocks + texts
            else:
                prev.extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for m in prepare_history(messages):
        if m.role == "user":
            push("user", [{"type": "text", "text": m.text}] if m.text else [])
        elif m.role == "tool":
            push("user", [{
                "type": "tool_result",
                "tool_use_id": m.tool_call_id,
                "content": [{"type": "text", "text": m.text or "(空结果)"}],
                "is_error": m.is_error,
            }])
        else:
            blocks: list[dict[str, Any]] = []
            echo = _server_echo(m, host)
            for c in m.content:
                if isinstance(c, ServerBlock):
                    if (c.data.get("id") or c.data.get("tool_use_id")) in echo:
                        blocks.append(dict(c.data))
                elif isinstance(c, ThinkingBlock):
                    # 只回传带签名的 Anthropic thinking；其它 provider 的思考无法被校验，丢弃
                    if m.provider == "anthropic" and c.signature:
                        if c.signature.startswith(_REDACTED):
                            blocks.append({"type": "redacted_thinking", "data": c.signature[len(_REDACTED):]})
                        else:
                            blocks.append({"type": "thinking", "thinking": c.thinking, "signature": c.signature})
                elif isinstance(c, ToolCall):
                    blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments or {}})
                elif getattr(c, "text", ""):
                    blocks.append({"type": "text", "text": c.text})
            push("assistant", blocks)
    return out


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(
        self, api_key: str | None = None, base_url: str | None = None, timeout: float = 600.0, *,
        effort: str | None = None, thinking: bool = True, thinking_display: str = "summarized",
        server_fallback: bool = True,
    ):
        try:
            import anthropic  # 延迟导入：不用 Anthropic 时无需安装该依赖
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("使用 Anthropic provider 需要安装官方 SDK：pip install 'anthropic>=1.0'") from e
        self._sdk = anthropic
        base_url = base_url or os.environ.get("ANTHROPIC_BASE_URL")
        self.client = anthropic.AsyncAnthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"),
            base_url=base_url,
            timeout=timeout,
            max_retries=0,  # 关键：重试只在 harness 一层做
        )
        self.effort = effort
        self.thinking = thinking
        self.thinking_display = thinking_display
        # server-side fallback 是 Anthropic 官方 API 的 beta：兼容网关（OpenRouter、DeepSeek 的 /anthropic 端点、
        # 自建代理）不认识 fallbacks: "default"，实测 OpenRouter 直接 400。只在官方端点上开启
        self.first_party = not base_url or base_url.rstrip("/").startswith("https://api.anthropic.com")
        self.server_fallback = server_fallback and self.first_party
        self.base_url = base_url
        self.host = provider_host(self)

    async def aclose(self) -> None:
        await self.client.close()

    def _kwargs(self, req: ModelRequest) -> tuple[dict[str, Any], list[str]]:
        info = self.model_info(req.model)
        kw: dict[str, Any] = {
            "model": req.model,
            "max_tokens": req.max_tokens,
            "system": req.system,
            "messages": to_anthropic_messages(req.messages, self.host),
            "stream": True,
            "cache_control": {"type": "ephemeral"},  # 自动前缀缓存
        }
        if req.tools:
            # 流式 + 自定义工具：开启 eager_input_streaming，大参数（文件内容）边生成边到达；
            # 代价是服务端不再校验参数——harness 的执行层本来就会做 schema 校验
            kw["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"],
                            "eager_input_streaming": True} for t in req.tools]
            kw["tool_choice"] = {"type": req.tool_choice}
        if (spec := self._native(req)) is not None:
            kw["tools"] = [*kw.get("tools", []), *server_tools(spec, req.native_opts)]
        if info.supports_thinking and self.thinking:
            kw["thinking"] = {"type": "adaptive", "display": self.thinking_display}
        elif req.temperature is not None:
            kw["temperature"] = req.temperature  # 新一代模型不接受采样参数，只在非 thinking 模型上传
        if self.effort:
            kw["output_config"] = {"effort": self.effort}
        betas: list[str] = []
        if self.server_fallback and info.server_fallback:
            betas.append("server-side-fallback-2026-07-01")
            kw["fallbacks"] = "default"
        return kw, betas

    def _native(self, req: ModelRequest):
        spec = NATIVE_SPECS.get(req.native_search or "")
        return spec if spec is not None and matches(spec, self, req.model) else None

    def _map_error(self, e: Exception) -> ProviderError:
        a = self._sdk
        if isinstance(e, a.APITimeoutError):
            return ProviderError(ErrorKind.TIMEOUT, str(e), provider=self.name)
        if isinstance(e, a.APIConnectionError):
            return ProviderError(ErrorKind.NETWORK, str(e), provider=self.name)
        if isinstance(e, a.APIStatusError):
            headers = dict(e.response.headers) if getattr(e, "response", None) is not None else {}
            return classify_http_error(e.status_code, str(getattr(e, "message", e)), headers, self.name)
        if isinstance(e, a.AnthropicError) and "api_key" in str(e).lower():
            return ProviderError(ErrorKind.AUTH, f"未配置 Anthropic 凭据：{e}", provider=self.name)
        # 其它客户端错误（响应解析失败、流被截断……）原因不明：按 UNKNOWN 上报，不冒充鉴权失败
        return ProviderError(ErrorKind.UNKNOWN, f"{type(e).__name__}: {e}", provider=self.name)

    async def stream(self, req: ModelRequest, cancel: CancelToken) -> AsyncIterator[StreamEvent]:
        kw, betas = self._kwargs(req)
        info = self.model_info(req.model)
        try:
            if betas:
                raw = await cancel.guard(self.client.beta.messages.create(**kw, betas=betas))
            else:
                raw = await cancel.guard(self.client.messages.create(**kw))
        except self._sdk.AnthropicError as e:
            raise self._map_error(e) from e

        b = MessageBuilder(self.name, req.model, self.host)
        usage = Usage()
        stop: str | None = None
        kinds: dict[int, str] = {}
        server_use: dict[str, Any] | None = None   # usage.server_tool_use：供应商侧工具的调用次数
        cited: list[str] = []
        try:
            yield StreamEvent("start")
            async for ev in cancel.iterate(raw.__aiter__()):
                t = ev.type
                if t == "message_start":
                    u = ev.message.usage
                    usage.input_tokens = u.input_tokens or 0
                    usage.cache_read_tokens = getattr(u, "cache_read_input_tokens", 0) or 0
                    usage.cache_write_tokens = getattr(u, "cache_creation_input_tokens", 0) or 0
                    b.model = getattr(ev.message, "model", None) or req.model  # fallback 时为实际服务的模型
                    server_use = _plain(getattr(u, "server_tool_use", None)) or server_use
                elif t == "content_block_start":
                    cb, i = ev.content_block, ev.index
                    kinds[i] = cb.type
                    if cb.type == "tool_use":
                        b.tool_start(i, cb.id, cb.name)
                        yield StreamEvent("toolcall_start", index=i, info={"id": cb.id, "name": cb.name})
                    elif cb.type == "text" and getattr(cb, "text", ""):
                        b.text(i, cb.text)
                        yield StreamEvent("text_delta", cb.text, i)
                    elif cb.type == "redacted_thinking":
                        b.signature(i, _REDACTED + cb.data)
                    elif cb.type == "server_tool_use":
                        b.server(i, {"type": cb.type, "id": cb.id, "name": cb.name,
                                     "input": _plain(getattr(cb, "input", None)) or {}})
                    elif cb.type.endswith("_tool_result") and getattr(cb, "tool_use_id", None):
                        b.server(i, _plain(cb))
                    elif cb.type == "fallback":
                        # 服务端拒答后切换到备用模型：边界之前的 thinking / tool_use 不能回传
                        b.drop_non_text()
                        yield StreamEvent("notice", info={"code": "server_fallback", "text": "主模型拒答，服务端已切换到备用模型继续"})
                elif t == "content_block_delta":
                    d, i = ev.delta, ev.index
                    if d.type == "text_delta":
                        b.text(i, d.text)
                        yield StreamEvent("text_delta", d.text, i)
                    elif d.type == "input_json_delta":
                        if not b.server_delta(i, d.partial_json):
                            b.tool_delta(i, d.partial_json)
                            yield StreamEvent("toolcall_delta", d.partial_json, i)
                    elif d.type == "citations_delta":
                        url = (_plain(getattr(d, "citation", None)) or {}).get("url")
                        if isinstance(url, str) and url not in cited:
                            cited.append(url)
                    elif d.type == "thinking_delta":
                        b.thinking(i, d.thinking)
                        yield StreamEvent("thinking_delta", d.thinking, i)
                    elif d.type == "signature_delta":
                        b.signature(i, d.signature)
                elif t == "content_block_stop":
                    if kinds.get(ev.index) == "tool_use":
                        yield StreamEvent("toolcall_end", index=ev.index, tool_call=b.tool_call(ev.index))
                elif t == "message_delta":
                    stop = getattr(ev.delta, "stop_reason", None) or stop
                    if getattr(ev, "usage", None) is not None and ev.usage.output_tokens is not None:
                        usage.output_tokens = ev.usage.output_tokens
                    if getattr(ev, "usage", None) is not None:
                        server_use = _plain(getattr(ev.usage, "server_tool_use", None)) or server_use
        except self._sdk.AnthropicError as e:
            raise self._map_error(e) from e
        finally:
            close = getattr(raw, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass
        usage.cost_usd = compute_cost(info, usage)   # 只算 token；搜索费由 loop 按声明单价计入
        msg = b.build(_STOP.get(stop or "end_turn", StopReason.END_TURN), usage)
        if (spec := self._native(req)) is not None:
            blocks = [c.data for c in msg.content if isinstance(c, ServerBlock)]
            if (nw := summarize(spec, blocks, server_use, cited)) is not None:
                msg.meta["native_web"] = nw
        yield StreamEvent("done", message=msg)
