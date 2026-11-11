"""回放 provider：按录制的轨迹重放 assistant 消息，实现 **确定性回放**。

非确定性的东西（LLM 输出、工具结果、时间）在首次执行时被记录下来；回放时从记录中读取，
而不是重新调用模型——这正是 Temporal 等持久执行引擎的核心思想，也是轨迹调试与回归测试的基础。

``ReplayProvider`` 还会比对“回放时模型看到的上下文”与“录制时的上下文”的哈希，
第一次不一致的位置就是 **分叉点**（例如改了系统提示词、工具描述或某个工具的返回）。
"""

from __future__ import annotations

from typing import AsyncIterator, Callable

from .._utils import short_hash
from ..cancel import CancelToken
from ..types import Message
from .base import ModelRequest, Provider, StreamEvent


def context_fingerprint(req: ModelRequest) -> str:
    """请求上下文指纹：system + tools + 消息（只取角色与内容），用于检测回放分叉。

    工具取名字 + 描述 + 参数 schema 的哈希：只改了一句工具描述，模型的行为也可能改变，回放必须能指出来。
    """
    fp: dict = {
        "system": req.system,
        "tools": [[t["name"], short_hash(t.get("description", "")), short_hash(t.get("parameters", {}))] for t in req.tools],
        "messages": [[m.role, m.text, [c.name for c in m.tool_calls], m.tool_call_id] for m in req.messages],
    }
    if req.native_search:   # 只在开了模型侧联网时出现：没开的请求指纹与以前相同
        fp["native_search"] = req.native_search
    return short_hash(fp)


class ReplayProvider(Provider):
    name = "replay"

    def __init__(self, recorded: list[Message], fingerprints: list[str] | None = None, chunk: int = 24,
                 summaries: list[str] | None = None, on_turn: Callable[[int], None] | None = None):
        self.recorded = [m for m in recorded if m.role == "assistant"]
        self.fingerprints = fingerprints or []
        self.chunk = chunk
        self.summaries = list(summaries or [])    # 录制时模型写的压缩摘要，按发生顺序
        self.on_turn = on_turn                    # 第 i 条录制回复结束时调用：录制时在这之后进来的外部输入由它补上
        self.cursor = 0
        self.summary_cursor = 0
        self.divergence: dict | None = None

    async def stream(self, req: ModelRequest, cancel: CancelToken) -> AsyncIterator[StreamEvent]:
        if req.purpose == "compaction":
            # 压缩摘要不在录制的 assistant 序列里，但它同样是模型的输出，要从录制里取：换成别的文字，后面每一步的上下文都会分叉。
            # 录制里没有（比如改了压缩阈值，回放时多压缩了一次）才用确定性摘要
            from ..types import TextBlock
            from .mock import deterministic_summary

            k = self.summary_cursor
            self.summary_cursor += 1
            text = self.summaries[k] if k < len(self.summaries) else deterministic_summary(req)
            yield StreamEvent("start")
            yield StreamEvent("done", message=Message(role="assistant", content=[TextBlock(text)], stop_reason="end_turn"))
            return
        i = self.cursor
        self.cursor += 1
        if i < len(self.fingerprints) and self.divergence is None:
            fp = context_fingerprint(req)
            if fp != self.fingerprints[i]:
                self.divergence = {"step": i, "expected": self.fingerprints[i], "actual": fp}
                yield StreamEvent("notice", info={"code": "replay_divergence", "text": f"第 {i} 步上下文与录制时不一致"})
        if i >= len(self.recorded):
            raise IndexError("回放轨迹已耗尽：当前执行比录制时多出了模型调用")
        msg = Message.from_dict(self.recorded[i].to_dict())
        yield StreamEvent("start")
        text = msg.text
        for j in range(0, len(text), self.chunk):
            cancel.raise_if_cancelled()
            yield StreamEvent("text_delta", text[j : j + self.chunk], 0)
        for k, c in enumerate(msg.tool_calls):
            yield StreamEvent("toolcall_start", index=k + 1, info={"id": c.id, "name": c.name})
            yield StreamEvent("toolcall_end", index=k + 1, tool_call=c)
        if self.on_turn is not None:
            self.on_turn(i)
        yield StreamEvent("done", message=msg)
