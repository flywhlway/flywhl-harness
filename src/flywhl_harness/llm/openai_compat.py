"""OpenAI Chat Completions 兼容 provider（DeepSeek、通义千问、Moonshot、智谱、Ollama、vLLM 等）。

直接用 httpx 读取 SSE 流，便于看清协议细节：

- 请求：``POST {base_url}/chat/completions``，``stream: true``，``stream_options.include_usage``；
- 增量：``choices[0].delta.content``（文本）、``delta.reasoning_content``（DeepSeek 等的推理过程）、
  ``delta.tool_calls[i]``（按 index 增量拼接 id / name / arguments 片段）；
- 结束：``finish_reason`` ∈ stop | tool_calls | length | content_filter，最后一个 chunk 携带 usage。

消息转换：tool 结果是独立的 ``role: tool`` 消息（与 Anthropic 放在 user 消息里的 tool_result 块不同）；
thinking 不回传（多数兼容服务要求不要把 reasoning_content 再发回去）。
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator

import httpx

from .._utils import new_id
from ..cancel import CancelToken
from ..types import Message, StopReason, ThinkingBlock, ToolCall, Usage
from .base import (
    ErrorKind, MessageBuilder, ModelRequest, Provider, ProviderError, StreamEvent, classify_http_error, prepare_history,
)
from .models import compute_cost

_FINISH = {
    "stop": StopReason.END_TURN,
    "tool_calls": StopReason.TOOL_USE,
    "function_call": StopReason.TOOL_USE,
    "length": StopReason.MAX_TOKENS,
    "content_filter": StopReason.REFUSAL,
}


def to_openai_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
    for m in prepare_history(messages):
        if m.role == "user":
            out.append({"role": "user", "content": m.text})
        elif m.role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.text or None}
            if m.tool_calls:
                msg["tool_calls"] = [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.name, "arguments": c.raw_arguments or json.dumps(c.arguments, ensure_ascii=False)}}
                    for c in m.tool_calls
                ]
            out.append(msg)
        else:  # tool
            content = m.text if not m.is_error else f"[工具执行出错]\n{m.text}"
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": content})
    return out


def to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
            for t in tools]


class OpenAICompatProvider(Provider):
    name = "openai"

    def __init__(self, api_key: str | None = None, base_url: str | None = None, timeout: float = 300.0,
                 extra_headers: dict[str, str] | None = None):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        # 分层超时：连接 15s；读取（相邻两个 chunk 的最大间隔）用于识别“流卡死”
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0, read=120.0))
        self.extra_headers = extra_headers or {}

    async def aclose(self) -> None:
        await self.client.aclose()

    def _payload(self, req: ModelRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": req.model,
            "messages": to_openai_messages(req.system, req.messages),
            "max_tokens": req.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if req.tools:
            body["tools"] = to_openai_tools(req.tools)
            body["tool_choice"] = req.tool_choice
        if req.temperature is not None:
            body["temperature"] = req.temperature
        return body

    async def stream(self, req: ModelRequest, cancel: CancelToken) -> AsyncIterator[StreamEvent]:
        if not self.api_key:
            raise ProviderError(ErrorKind.AUTH, "未配置 OPENAI_API_KEY（或 DEEPSEEK_API_KEY）", provider=self.name)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", **self.extra_headers}
        b = MessageBuilder(self.name, req.model)
        info = self.model_info(req.model)
        usage = Usage()
        finish: str | None = None
        tool_index: dict[int, int] = {}   # OpenAI 的 tool_calls[i].index → 我们的块序号
        text_idx = 0
        next_idx = 1
        try:
            async with self.client.stream("POST", f"{self.base_url}/chat/completions", json=self._payload(req), headers=headers) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", errors="replace")
                    raise classify_http_error(resp.status_code, body, dict(resp.headers), self.name)
                yield StreamEvent("start")
                async for line in cancel.iterate(resp.aiter_lines()):
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError as e:  # 流被截断或代理损坏：按网络错误归类，交给重试层
                        raise ProviderError(ErrorKind.NETWORK, f"无法解析 SSE 数据块：{e}", provider=self.name) from e
                    if chunk.get("usage"):
                        u = chunk["usage"]
                        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or u.get("prompt_cache_hit_tokens", 0)
                        usage = Usage(input_tokens=u.get("prompt_tokens", 0) - cached, output_tokens=u.get("completion_tokens", 0),
                                      cache_read_tokens=cached)
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("reasoning_content"):
                            b.thinking(0, delta["reasoning_content"])
                            yield StreamEvent("thinking_delta", delta["reasoning_content"], 0)
                        if delta.get("content"):
                            if text_idx == 0:
                                text_idx = next_idx
                                next_idx += 1
                            b.text(text_idx, delta["content"])
                            yield StreamEvent("text_delta", delta["content"], text_idx)
                        for tc in delta.get("tool_calls") or []:
                            oi = tc.get("index", 0)
                            fn = tc.get("function") or {}
                            if oi not in tool_index:
                                tool_index[oi] = next_idx
                                next_idx += 1
                                b.tool_start(tool_index[oi], tc.get("id") or new_id("call_"), fn.get("name") or "")
                                yield StreamEvent("toolcall_start", index=tool_index[oi], info={"id": tc.get("id"), "name": fn.get("name")})
                            if fn.get("arguments"):
                                b.tool_delta(tool_index[oi], fn["arguments"])
                                yield StreamEvent("toolcall_delta", fn["arguments"], tool_index[oi])
                        if choice.get("finish_reason"):
                            finish = choice["finish_reason"]
        except httpx.TimeoutException as e:
            raise ProviderError(ErrorKind.TIMEOUT, f"请求超时：{e!r}", provider=self.name) from e
        except httpx.TransportError as e:
            raise ProviderError(ErrorKind.NETWORK, f"网络错误：{e!r}", provider=self.name) from e
        for i in tool_index.values():
            yield StreamEvent("toolcall_end", index=i, tool_call=b.tool_call(i))
        usage.cost_usd = compute_cost(info, usage)
        msg = b.build(_FINISH.get(finish or "stop", StopReason.END_TURN), usage)
        # 思考内容只用于展示：兼容服务不接受回传，去掉签名避免误用
        msg.content = [c if not isinstance(c, ThinkingBlock) else ThinkingBlock(c.thinking, None) for c in msg.content]
        yield StreamEvent("done", message=msg)


__all__ = ["OpenAICompatProvider", "to_openai_messages", "to_openai_tools", "ToolCall"]
