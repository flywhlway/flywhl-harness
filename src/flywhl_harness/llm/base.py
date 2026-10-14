"""模型接入层的抽象：harness 与各家 provider 之间的“防腐层”。

所有 provider 都被归一化为同一套流式事件 ``StreamEvent``：

    start → (text_delta | thinking_delta | toolcall_start | toolcall_delta | toolcall_end)* → done

约定（借鉴 pi-ai）：
- ``stream()`` 产出增量事件，最后一个事件是 ``done``，携带完整的 assistant ``Message``；
- 可恢复的失败（限流、过载、超时）以 ``ProviderError`` 抛出，并带上 **错误分类**，
  由上层统一决定重试/降级——provider 自己 **不做重试**（全链路只允许一层负责重试，否则 3 层 × 3 次 = 27 倍放大）；
- 取消通过 ``CancelToken`` 传入，provider 在读取流时检查并尽快断开连接。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, AsyncIterator

from .._utils import safe_json_loads
from ..cancel import CancelToken
from ..types import Message, StopReason, TextBlock, ThinkingBlock, ToolCall, Usage
from .models import ModelInfo, get_model_info


@dataclass
class ModelRequest:
    model: str
    system: str
    messages: list[Message]
    tools: list[dict[str, Any]] = field(default_factory=list)
    max_tokens: int = 8192
    temperature: float | None = None
    # auto：模型自行决定是否调用工具；none：本轮禁止调用（收尾/摘要）。
    # 注意：收尾时 **保留** tools 定义、只改 tool_choice，而不是删掉工具——
    # 删工具会改变请求前缀（缓存失效，且部分 provider 要求历史中出现过的工具必须有定义）。
    tool_choice: str = "auto"
    purpose: str = "turn"  # turn | wrap_up | compaction | subagent
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StreamEvent:
    type: str  # start | text_delta | thinking_delta | toolcall_start | toolcall_delta | toolcall_end | notice | retry | done
    delta: str = ""
    index: int = 0
    tool_call: ToolCall | None = None
    message: Message | None = None
    info: dict[str, Any] = field(default_factory=dict)


class ErrorKind(StrEnum):
    RATE_LIMIT = "rate_limit"            # 429：遵守 Retry-After 退避
    OVERLOADED = "overloaded"            # 529/503：有限次重试后降级
    SERVER = "server"                    # 5xx
    TIMEOUT = "timeout"                  # 连接/首 token/空闲超时
    NETWORK = "network"                  # 连接断开
    CONTEXT_OVERFLOW = "context_overflow"  # 上下文超长：不能原样重试，要先压缩
    BAD_REQUEST = "bad_request"          # 400：消息结构非法等，原样重试无意义
    AUTH = "auth"                        # 401/403：告警，不重试
    NOT_FOUND = "not_found"              # 模型不存在
    UNKNOWN = "unknown"


RETRYABLE_KINDS = frozenset({ErrorKind.RATE_LIMIT, ErrorKind.OVERLOADED, ErrorKind.SERVER, ErrorKind.TIMEOUT, ErrorKind.NETWORK})


class ProviderError(Exception):
    def __init__(
        self, kind: ErrorKind, message: str, *, status: int | None = None,
        retry_after: float | None = None, provider: str | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after
        self.provider = provider

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE_KINDS

    def __str__(self) -> str:
        s = f" HTTP {self.status}" if self.status else ""
        return f"[{self.kind}{s}] {self.args[0]}"


_OVERFLOW_HINTS = ("context_length", "context length", "maximum context", "too long", "prompt is too long",
                   "max_tokens_exceeded", "exceeds the context", "reduce the length")


def classify_http_error(status: int, body: str, headers: dict[str, str] | None = None, provider: str | None = None) -> ProviderError:
    """把 HTTP 错误归类。分类决定策略：重试同样的请求、改变输入后重试、还是不重试。"""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    retry_after = None
    if "retry-after" in headers:
        try:
            retry_after = float(headers["retry-after"])
        except ValueError:
            retry_after = None
    low = body.lower()
    if status == 429:
        kind = ErrorKind.RATE_LIMIT
    elif status in (529, 503) or "overloaded" in low:
        kind = ErrorKind.OVERLOADED
    elif status >= 500:
        kind = ErrorKind.SERVER
    elif status in (401, 403):
        kind = ErrorKind.AUTH
    elif status == 404:
        kind = ErrorKind.NOT_FOUND
    elif status in (400, 413, 422) and any(h in low for h in _OVERFLOW_HINTS):
        kind = ErrorKind.CONTEXT_OVERFLOW
    elif status == 408:
        kind = ErrorKind.TIMEOUT
    elif 400 <= status < 500:
        kind = ErrorKind.BAD_REQUEST
    else:
        kind = ErrorKind.UNKNOWN
    return ProviderError(kind, body[:500] or f"HTTP {status}", status=status, retry_after=retry_after, provider=provider)


class MessageBuilder:
    """把流式增量拼装成完整的 assistant 消息。

    工具参数以 partial JSON 增量到达：可以边收边展示，但 **只有在块完整、解析成功之后才能执行**。
    解析失败时保留原文与错误，交给执行层回给模型修正。
    """

    def __init__(self, provider: str, model: str):
        self.provider = provider
        self.model = model
        self._blocks: dict[int, dict[str, Any]] = {}

    def text(self, index: int, delta: str) -> None:
        b = self._blocks.setdefault(index, {"type": "text", "text": ""})
        b["text"] += delta

    def thinking(self, index: int, delta: str) -> None:
        b = self._blocks.setdefault(index, {"type": "thinking", "thinking": "", "signature": None})
        b["thinking"] += delta

    def signature(self, index: int, sig: str) -> None:
        b = self._blocks.setdefault(index, {"type": "thinking", "thinking": "", "signature": None})
        b["signature"] = (b.get("signature") or "") + sig

    def tool_start(self, index: int, call_id: str, name: str) -> None:
        self._blocks[index] = {"type": "tool_call", "id": call_id, "name": name, "json": ""}

    def tool_delta(self, index: int, partial: str) -> None:
        b = self._blocks.get(index)
        if b is not None and b["type"] == "tool_call":
            b["json"] += partial

    def tool_call(self, index: int) -> ToolCall | None:
        b = self._blocks.get(index)
        if b is None or b["type"] != "tool_call":
            return None
        args, err = safe_json_loads(b["json"])
        if err is None and not isinstance(args, dict):
            err, args = "工具参数必须是 JSON 对象", None
        return ToolCall(b["id"], b["name"], args or {}, raw_arguments=b["json"] if err else None, parse_error=err)

    def drop_non_text(self) -> None:
        """丢弃非文本块（用于 server-side fallback 的边界：之前的 thinking/tool_use 不能回传）。"""
        self._blocks = {k: v for k, v in self._blocks.items() if v["type"] == "text"}

    def build(self, stop_reason: str, usage: Usage | None = None, *, error: str | None = None, partial: bool = False) -> Message:
        content: list[Any] = []
        for idx in sorted(self._blocks):
            b = self._blocks[idx]
            if b["type"] == "text" and b["text"]:
                content.append(TextBlock(b["text"]))
            elif b["type"] == "thinking" and not partial:
                # 半截 thinking 没有签名，回传会被拒：被中断的消息只保留文本
                content.append(ThinkingBlock(b["thinking"], b.get("signature")))
            elif b["type"] == "tool_call" and not partial:
                # 半截 tool_use 必须丢弃：参数不完整，执行它是危险的
                call = self.tool_call(idx)
                if call:
                    content.append(call)
        return Message(
            role="assistant", content=content, stop_reason=stop_reason, usage=usage,
            model=self.model, provider=self.provider, error=error,
        )


class Provider(ABC):
    """模型 provider 的统一接口。"""

    name: str = "base"

    def model_info(self, model: str) -> ModelInfo:
        return get_model_info(model, self.name)

    @abstractmethod
    def stream(self, req: ModelRequest, cancel: CancelToken) -> AsyncIterator[StreamEvent]:
        """流式调用模型。最后一个事件必须是 ``done``（携带完整消息）。"""

    async def complete(self, req: ModelRequest, cancel: CancelToken | None = None) -> Message:
        """便捷方法：消费整个流，返回最终消息（压缩摘要等非交互场景使用）。"""
        cancel = cancel or CancelToken()
        final: Message | None = None
        async for ev in self.stream(req, cancel):
            if ev.type == "done":
                final = ev.message
        if final is None:
            raise ProviderError(ErrorKind.UNKNOWN, "provider 未返回 done 事件", provider=self.name)
        return final

    async def aclose(self) -> None:
        return None


# ====================================================================== 通用预处理
def prepare_history(messages: list[Message]) -> list[Message]:
    """发送前的通用修补：保证消息结构合法（tool_call 与 tool_result 严格配对）。

    - 跳过 error / refusal 的 assistant 消息（它们没有可复用的内容）；
    - aborted 的 assistant 只保留文本（构建时已丢弃半截 thinking 与工具调用）；
    - 孤立的 tool 结果（找不到对应调用）直接丢弃；
    - 不在末尾却缺少结果的工具调用，补一条合成的错误结果（pi 的做法），避免 API 直接 400。

    这些规则对每次请求 **完全一致**，因此不会破坏“只追加”的前缀稳定性。
    """
    out: list[Message] = []
    pending: dict[str, ToolCall] = {}

    def flush_pending() -> None:
        for call in pending.values():
            out.append(Message(role="tool", content=[TextBlock("[没有返回结果：该调用被中断或丢失]")],
                               tool_call_id=call.id, tool_name=call.name, is_error=True))
        pending.clear()

    for m in messages:
        if m.role == "assistant":
            if m.stop_reason in (StopReason.ERROR, StopReason.REFUSAL):
                continue
            if not m.content:
                continue
            flush_pending()
            out.append(m)
            pending = {c.id: c for c in m.tool_calls}
        elif m.role == "tool":
            if m.tool_call_id in pending:
                pending.pop(m.tool_call_id)
                out.append(m)
        else:
            if pending and m.kind not in ("reminder",):
                flush_pending()
            out.append(m)
    # 末尾仍未配对的调用：由 Loop 负责执行或修补，这里不合成
    return out


def tool_specs_json(tools: list[dict[str, Any]]) -> str:
    return json.dumps(tools, ensure_ascii=False, sort_keys=True)
