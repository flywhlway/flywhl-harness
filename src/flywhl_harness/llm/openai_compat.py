"""OpenAI Chat Completions 兼容 provider（DeepSeek、通义千问、Kimi、智谱、MiniMax、OpenRouter、Ollama、vLLM 等）。

直接用 httpx 读取 SSE 流，便于看清协议细节：

- 请求：``POST {base_url}/chat/completions``，``stream: true``，``stream_options.include_usage``；
- 增量：``choices[0].delta.content``（文本）、``delta.reasoning_content`` / ``delta.reasoning``（思考过程）、
  ``delta.tool_calls[i]``（按 index 增量拼接 id / name / arguments 片段，id 只在首个分片出现）；
- 结束：``finish_reason`` ∈ stop | tool_calls | length | content_filter，最后一个 chunk 携带 usage；
  ``: keep-alive`` 这类 SSE 注释行直接跳过。

各家差异收敛在 ``ProviderProfile`` 里（见 ``profiles.py``），这里只按档案行事：

- **思考内容回传**：DeepSeek / Kimi 的思考模型在带 tools 的多轮请求中要求把 ``reasoning_content`` 原样
  回传（否则可能 400，且模型会丢掉上一步的推理）；MiniMax 把 ``<think>`` 内联在正文里，回传整段正文；
  其余服务不回传。只回传 **本供应商** 产生的思考——别家的思考对它没有意义。
- **流的完整性**：没有收到 ``finish_reason`` 就断开的流按网络错误处理（交给重试层），绝不当作“正常结束”；
  流中途下发的 ``error`` 对象、DeepSeek 的 ``insufficient_system_resource`` 同样转成可分类的错误。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from .._utils import new_id
from ..cancel import CancelToken
from ..types import Message, StopReason, ThinkingBlock, ToolCall, Usage
from .base import (
    ErrorKind, MessageBuilder, ModelRequest, Provider, ProviderError, StreamEvent, classify_http_error, prepare_history,
)
from .models import ModelInfo, compute_cost, get_model_info
from .profiles import PROFILES, ProviderProfile

_FINISH = {
    "stop": StopReason.END_TURN,
    "end_turn": StopReason.END_TURN,
    "tool_calls": StopReason.TOOL_USE,
    "function_call": StopReason.TOOL_USE,
    "length": StopReason.MAX_TOKENS,
    "content_filter": StopReason.REFUSAL,
}
_THINK_OPEN, _THINK_CLOSE = "<think>", "</think>"


def to_openai_messages(system: str, messages: list[Message], *, echo: str = "none",
                       provider: str | None = None) -> list[dict[str, Any]]:
    """统一消息 → Chat Completions 消息。``echo`` 决定思考内容如何回传（见模块文档）。"""
    out: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
    for m in prepare_history(messages):
        if m.role == "user":
            out.append({"role": "user", "content": m.text})
        elif m.role == "assistant":
            own = provider is None or m.provider == provider
            thinking = m.thinking if own else ""
            text = m.text
            if echo == "inline" and thinking:
                text = f"{_THINK_OPEN}\n{thinking}\n{_THINK_CLOSE}\n\n{text}"
            msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if echo == "field":
                msg["reasoning_content"] = thinking
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


class ThinkTagSplitter:
    """把正文开头内联的 ``<think>…</think>`` 拆成思考与正文（MiniMax、部分本地模型）。

    只处理消息 **开头** 的思考块：正文里讨论 ``<think>`` 标签的内容不受影响。标签可能被切在两个分片之间，
    所以疑似标签前缀的尾巴先留在缓冲区里，等下一个分片再判断。
    """

    def __init__(self) -> None:
        self.state = "start"   # start → think → text
        self.buf = ""
        self._trim = False     # 思考块之后的空行属于格式，不进正文

    def feed(self, s: str) -> list[tuple[str, str]]:
        self.buf += s
        out: list[tuple[str, str]] = []
        if self.state == "start":
            head = self.buf.lstrip()
            if not head or (_THINK_OPEN.startswith(head) and len(head) < len(_THINK_OPEN)):
                return out                          # 还看不出来（可能是 "<thi" 这样的半截标签），等下一个分片
            if head.startswith(_THINK_OPEN):
                self.state, self.buf = "think", head[len(_THINK_OPEN):].lstrip("\n")
            else:
                self.state = "text"
        if self.state == "think":
            i = self.buf.find(_THINK_CLOSE)
            if i < 0:
                keep = next((k for k in range(len(_THINK_CLOSE) - 1, 0, -1) if self.buf.endswith(_THINK_CLOSE[:k])), 0)
                emit, self.buf = self.buf[:len(self.buf) - keep], self.buf[len(self.buf) - keep:]
                if emit:
                    out.append(("thinking", emit))
                return out
            if self.buf[:i]:
                out.append(("thinking", self.buf[:i]))
            self.state, self.buf, self._trim = "text", self.buf[i + len(_THINK_CLOSE):], True
        if self._trim:
            self.buf = self.buf.lstrip("\n")
            if not self.buf:
                return out
            self._trim = False
        if self.buf:
            out.append(("text", self.buf))
            self.buf = ""
        return out

    def flush(self) -> list[tuple[str, str]]:
        rest, self.buf = self.buf, ""
        if not rest or (self._trim and not rest.strip("\n")):
            return []
        return [("thinking" if self.state == "think" else "text", rest)]


class OpenAICompatProvider(Provider):
    """按 ``ProviderProfile`` 接入任意 Chat Completions 兼容服务。``name`` 即供应商名（deepseek、kimi……）。"""

    def __init__(self, api_key: str | None = None, base_url: str | None = None, timeout: float = 300.0,
                 extra_headers: dict[str, str] | None = None, *, profile: ProviderProfile | str = "openai",
                 thinking: str | None = None, reasoning_effort: str | None = None,
                 extra_body: dict[str, Any] | None = None):
        self.profile = PROFILES[profile] if isinstance(profile, str) else profile
        self.name = self.profile.name
        self.reports_cost = self.profile.reports_cost
        self.api_key = api_key or self.profile.api_key()
        self.base_url = (base_url or self.profile.resolved_base_url() or "https://api.openai.com/v1").rstrip("/")
        # 分层超时：连接 15s；读取（相邻两个 chunk 的最大间隔）用于识别“流卡死”
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0, read=120.0))
        self.extra_headers = extra_headers or {}
        self.options = {**self.profile.request_options(thinking, reasoning_effort), **(extra_body or {})}

    def model_info(self, model: str) -> ModelInfo:
        return get_model_info(model, self.name)

    async def aclose(self) -> None:
        await self.client.aclose()

    def _payload(self, req: ModelRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": req.model,
            "messages": to_openai_messages(req.system, req.messages, echo=self.profile.reasoning_echo, provider=self.name),
            "max_tokens": req.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if req.tools:
            body["tools"] = to_openai_tools(req.tools)
            body["tool_choice"] = req.tool_choice
        if req.temperature is not None:
            body["temperature"] = req.temperature
        body.update(self.options)
        return body

    def _usage(self, u: dict[str, Any], info: ModelInfo) -> Usage:
        # 缓存命中的字段名各家不同：OpenAI/通义 prompt_tokens_details.cached_tokens、DeepSeek prompt_cache_hit_tokens、
        # Kimi 顶层 cached_tokens。prompt_tokens 都包含命中部分，这里拆开以便分别计价
        cached = ((u.get("prompt_tokens_details") or {}).get("cached_tokens") or u.get("prompt_cache_hit_tokens")
                  or u.get("cached_tokens") or 0)
        usage = Usage(input_tokens=max(0, (u.get("prompt_tokens") or 0) - cached),
                      output_tokens=u.get("completion_tokens") or 0, cache_read_tokens=cached)
        reported = u.get("cost")
        usage.cost_usd = float(reported) if self.profile.reports_cost and isinstance(reported, (int, float)) \
            else compute_cost(info, usage)
        return usage

    async def stream(self, req: ModelRequest, cancel: CancelToken) -> AsyncIterator[StreamEvent]:
        if self.profile.needs_key and not self.api_key:
            envs = " / ".join(self.profile.key_env) or "api_key"
            raise ProviderError(ErrorKind.AUTH, f"未配置 {self.profile.label} 的密钥（{envs}）", provider=self.name)
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        b = MessageBuilder(self.name, req.model)
        info = self.model_info(req.model)
        usage = Usage()
        finish: str | None = None
        tool_index: dict[int, int] = {}   # OpenAI 的 tool_calls[i].index → 我们的块序号
        text_idx = 0
        next_idx = 1
        splitter = ThinkTagSplitter() if self.profile.think_tags else None

        def emit_text(delta: str) -> StreamEvent:
            nonlocal text_idx, next_idx
            if text_idx == 0:
                text_idx = next_idx
                next_idx += 1
            b.text(text_idx, delta)
            return StreamEvent("text_delta", delta, text_idx)

        def emit_thinking(delta: str) -> StreamEvent:
            b.thinking(0, delta)
            return StreamEvent("thinking_delta", delta, 0)

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
                    if chunk.get("error"):        # 流中途下发的错误（OpenRouter 等）：按 HTTP 错误同样归类
                        err = chunk["error"]
                        code = err.get("code") if isinstance(err, dict) else None
                        status = code if isinstance(code, int) and code >= 400 else 500
                        raise classify_http_error(status, json.dumps(err, ensure_ascii=False), None, self.name)
                    if chunk.get("model"):
                        b.model = chunk["model"]   # 实际服务的模型（旧名映射、路由服务都会改写它）
                    if chunk.get("usage"):
                        usage = self._usage(chunk["usage"], info)
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if isinstance(reasoning, str) and reasoning:
                            yield emit_thinking(reasoning)
                        if delta.get("content"):
                            parts = splitter.feed(delta["content"]) if splitter else [("text", delta["content"])]
                            for kind, piece in parts:
                                yield emit_thinking(piece) if kind == "thinking" else emit_text(piece)
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
        if finish is None:
            raise ProviderError(ErrorKind.NETWORK, "流在收到 finish_reason 之前结束（连接中断或被代理截断）", provider=self.name)
        if finish == "insufficient_system_resource":
            raise ProviderError(ErrorKind.OVERLOADED, "服务端资源不足，生成被中断（insufficient_system_resource）", provider=self.name)
        for kind, piece in splitter.flush() if splitter else []:
            yield emit_thinking(piece) if kind == "thinking" else emit_text(piece)
        for i in tool_index.values():
            yield StreamEvent("toolcall_end", index=i, tool_call=b.tool_call(i))
        msg = b.build(_FINISH.get(finish, StopReason.END_TURN), usage)
        # 兼容服务的思考没有签名：去掉签名字段，避免被当作 Anthropic thinking 回传
        msg.content = [c if not isinstance(c, ThinkingBlock) else ThinkingBlock(c.thinking, None) for c in msg.content]
        yield StreamEvent("done", message=msg)


__all__ = ["OpenAICompatProvider", "ThinkTagSplitter", "to_openai_messages", "to_openai_tools", "ToolCall"]
