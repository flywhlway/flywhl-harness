"""Harness 事件模型。

Agent Loop 是一个 async generator，它“产出”的就是这些事件；CLI、Web UI、SDK、上层 Agent
都只是事件的消费者。事件同时承担三种职责：

- **流式展示**：``message_update`` / ``tool_update`` 等增量事件驱动 UI；
- **断线续传**：每个事件在 Run 内有单调递增的 ``seq``，客户端凭 ``Last-Event-ID`` 补齐；
- **审计与回放**：事件落库后就是一条完整的执行轨迹（trajectory）。

事件命名参考了 pi 的 10 种 AgentEvent（agent/turn/message/tool_execution 的 start/update/end），
并补充了 harness 运行时特有的状态、审批、压缩、重试与子代理事件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ._utils import now


class E:
    """事件类型常量。用字符串而非 Enum：天然可 JSON 序列化、可直接作为 SSE 的 event 名。"""

    # ---- Run 生命周期 ----
    RUN_START = "run_start"
    RUN_END = "run_end"              # data: status, reason, usage
    STATUS = "status"                # Run 状态机迁移：data: from, to, reason, by
    # ---- 回合 ----
    TURN_START = "turn_start"        # data: turn
    TURN_END = "turn_end"
    # ---- 消息 ----
    MESSAGE_START = "message_start"  # data: message_id, role
    MESSAGE_UPDATE = "message_update"  # data: message_id, kind(text|thinking|toolcall), delta, index
    MESSAGE_END = "message_end"      # data: message（完整消息字典）
    MODEL_RETRY = "model_retry"      # data: attempt, delay, error, discard_partial
    # ---- 工具 ----
    TOOL_START = "tool_start"        # data: call_id, name, args, summary, effect
    TOOL_UPDATE = "tool_update"      # data: call_id, update（进度/部分输出）
    TOOL_END = "tool_end"            # data: call_id, name, status, is_error, content, details, duration_ms
    # ---- 策略 ----
    APPROVAL_REQUEST = "approval_request"
    APPROVAL_RESOLVED = "approval_resolved"
    # ---- 上下文 ----
    COMPACTION_START = "compaction_start"
    COMPACTION_END = "compaction_end"
    # ---- 其它 ----
    NOTICE = "notice"                # 预算/循环/恢复等提示：data: level, code, text
    USAGE = "usage"                  # 预算快照
    ERROR = "error"


# 高频增量事件：可以合并、可以在慢客户端处丢弃（客户端会通过 message_end 拿到完整结果）
EPHEMERAL_EVENTS = frozenset({E.MESSAGE_UPDATE, E.TOOL_UPDATE})


@dataclass
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    seq: int | None = None  # 由 EventHub 统一分配：Run 内单调递增，(run_id, seq) 唯一
    ts: float = field(default_factory=now)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "data": self.data, "run_id": self.run_id, "seq": self.seq, "ts": self.ts}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        return cls(d["type"], d.get("data") or {}, d.get("run_id"), d.get("seq"), d.get("ts") or now())

    def __repr__(self) -> str:  # 调试友好
        return f"Event({self.seq}:{self.type} {list(self.data)[:4]})"
