"""provider 无关的统一消息模型。

设计要点
--------
1. **三种角色**：``user`` / ``assistant`` / ``tool``。system 提示不进消息列表，单独放在请求里，
   保证消息历史是“只追加”的，从而让前缀缓存稳定。
2. **内容块**：assistant 的一条消息可以同时包含文本、思考（thinking）与多个工具调用；
   每个 ``ToolCall`` 都必须在后续有一条 ``tool`` 消息与之 **按 id 一一配对**，否则下一次请求会被 API 拒绝。
3. **content 与 details 分离**（借鉴 pi）：``tool`` 消息的 ``content`` 给模型看，``details`` 只给 UI/审计，
   这样 UI 可以展示 diff、进度、完整路径等丰富信息，而不污染模型上下文。
4. **meta.kind** 标记消息来源（prompt / steer / reminder / compaction ...），便于上下文构建、UI 渲染与回放。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Union

from ._utils import new_id, now


@dataclass
class TextBlock:
    text: str
    type: Literal["text"] = "text"


@dataclass
class ThinkingBlock:
    thinking: str
    # Anthropic 的思考签名：同一 provider 内续写时必须原样回传；跨 provider 时只能降级为纯文本或丢弃
    signature: str | None = None
    type: Literal["thinking"] = "thinking"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    # 参数 JSON 解析失败时保留原文：harness 要把“哪里错了”告诉模型，而不是吞掉
    raw_arguments: str | None = None
    parse_error: str | None = None
    type: Literal["tool_call"] = "tool_call"


Block = Union[TextBlock, ThinkingBlock, ToolCall]


@dataclass
class Usage:
    """一次模型调用的用量。以 provider 返回值为准，本地 tokenizer 只做预估。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def context_tokens(self) -> int:
        """本次请求实际占用的上下文大小（输入侧全部 token）。用于上下文水位判断。"""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
            round(self.cost_usd + other.cost_usd, 8),
        )

    def __sub__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens - other.input_tokens,
            self.output_tokens - other.output_tokens,
            self.cache_read_tokens - other.cache_read_tokens,
            self.cache_write_tokens - other.cache_write_tokens,
            round(self.cost_usd - other.cost_usd, 8),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StopReason:
    """assistant 消息的结束原因（统一不同 provider 的叫法）。"""

    END_TURN = "end_turn"      # 自然结束
    TOOL_USE = "tool_use"      # 请求调用工具
    MAX_TOKENS = "max_tokens"  # 输出被 max_tokens 截断：此时工具参数可能不完整！
    ABORTED = "aborted"        # 被取消（流式中途打断）
    ERROR = "error"            # 调用失败
    REFUSAL = "refusal"        # 安全策略拒答：必须先检查它再读内容，且不能执行其中的工具调用


class MsgKind:
    """消息来源标记（meta.kind）。"""

    PROMPT = "prompt"            # 用户发起本次 Run 的输入
    STEER = "steer"              # 运行中插话（下一个安全点注入）
    FOLLOW_UP = "follow_up"      # 排队到“本该停止时”再注入
    REMINDER = "reminder"        # harness 注入的 system-reminder（预算、循环、todo 等）
    COMPACTION = "compaction"    # 压缩摘要
    RESUME_NOTE = "resume_note"  # 恢复说明：暂停/崩溃期间发生了什么
    STOP_FEEDBACK = "stop_feedback"  # Stop 钩子阻止停止时给出的缺口
    WRAP_UP = "wrap_up"          # 预算耗尽前的收尾指令


Role = Literal["user", "assistant", "tool"]


@dataclass
class Message:
    role: Role
    content: list[Block]
    id: str = field(default_factory=lambda: new_id("m_"))
    ts: float = field(default_factory=now)
    # ---- assistant 专属 ----
    stop_reason: str | None = None
    usage: Usage | None = None
    model: str | None = None
    provider: str | None = None
    error: str | None = None
    # ---- tool 专属 ----
    tool_call_id: str | None = None
    tool_name: str | None = None
    is_error: bool = False
    details: dict[str, Any] | None = None  # 只给 UI/审计，绝不送进模型
    # ---- 通用 ----
    meta: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ 便捷属性
    @property
    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))

    @property
    def thinking(self) -> str:
        return "".join(b.thinking for b in self.content if isinstance(b, ThinkingBlock))

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [b for b in self.content if isinstance(b, ToolCall)]

    @property
    def kind(self) -> str | None:
        return self.meta.get("kind")

    # ------------------------------------------------------------------ 序列化
    def to_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id,
            "role": self.role,
            "ts": self.ts,
            "content": [asdict(b) for b in self.content],
            "meta": self.meta,
        }
        if self.role == "assistant":
            d.update(
                stop_reason=self.stop_reason,
                usage=self.usage.to_dict() if self.usage else None,
                model=self.model,
                provider=self.provider,
                error=self.error,
            )
        if self.role == "tool":
            d.update(
                tool_call_id=self.tool_call_id,
                tool_name=self.tool_name,
                is_error=self.is_error,
                details=self.details,
            )
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Message":
        blocks: list[Block] = []
        for b in d.get("content", []):
            t = b.get("type")
            if t == "text":
                blocks.append(TextBlock(b["text"]))
            elif t == "thinking":
                blocks.append(ThinkingBlock(b.get("thinking", ""), b.get("signature")))
            elif t == "tool_call":
                blocks.append(
                    ToolCall(b["id"], b["name"], b.get("arguments") or {}, b.get("raw_arguments"), b.get("parse_error"))
                )
        usage = d.get("usage")
        return cls(
            role=d["role"],
            content=blocks,
            id=d.get("id") or new_id("m_"),
            ts=d.get("ts") or now(),
            stop_reason=d.get("stop_reason"),
            usage=Usage(**usage) if usage else None,
            model=d.get("model"),
            provider=d.get("provider"),
            error=d.get("error"),
            tool_call_id=d.get("tool_call_id"),
            tool_name=d.get("tool_name"),
            is_error=bool(d.get("is_error", False)),
            details=d.get("details"),
            meta=d.get("meta") or {},
        )


# ---------------------------------------------------------------------- 构造函数
def user_message(text: str, kind: str = MsgKind.PROMPT, **meta: Any) -> Message:
    return Message(role="user", content=[TextBlock(text)], meta={"kind": kind, **meta})


def tool_result_message(
    call: ToolCall, content: str, *, is_error: bool = False, details: dict | None = None, **meta: Any
) -> Message:
    return Message(
        role="tool",
        content=[TextBlock(content)],
        tool_call_id=call.id,
        tool_name=call.name,
        is_error=is_error,
        details=details,
        meta=meta,
    )


def unpaired_tool_calls(messages: list[Message]) -> list[ToolCall]:
    """返回最后一条 assistant 消息中尚未得到结果的工具调用。

    这是 harness 的一条硬不变量：tool_call 与 tool_result 必须成对。
    崩溃恢复、取消、压缩之前都要用它检查并修补。
    """
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.role == "assistant":
            answered = {x.tool_call_id for x in messages[i + 1 :] if x.role == "tool"}
            return [c for c in m.tool_calls if c.id not in answered]
    return []
