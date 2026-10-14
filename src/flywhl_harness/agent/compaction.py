"""上下文压缩（compaction）：把较早的历史改写为结构化摘要。

压缩是上下文管理中 **最后一道、且有损** 的手段（前面还有：工具结果截断外置、旧结果清理、
todo/文件外置记忆、子代理隔离）。所以它必须按“恢复工作所需的最小集”来保留信息。

算法（借鉴 pi 的切点规则）
--------------------------
1. **触发**：估算水位 > min(窗口 × 阈值, 窗口 − 预留)；只在回合边界、所有 tool_call 都已配对时触发；
2. **切点**：从最新消息往回累加 token，直到达到 ``keep_recent_tokens``；然后把切点向后挪到合法边界——
   优先挪到一条用户消息上，其次是 assistant 消息；**绝不在 tool 结果处切**（否则结果会和它的调用分离）；
3. **摘要**：把切点之前的消息序列化为文本，交给模型按固定模板生成摘要；已有摘要时做 **增量更新**
   （避免“摘要的摘要”导致的信息衰减）；
4. **确定性补充**：原始任务原文、读过/改过的文件清单由代码直接提取，不依赖模型记忆；
5. **落盘**：追加一个 compaction 条目（摘要 + first_kept_entry_id + 前后 token 数）；原始历史保留可回查。

模型调用失败时退化为确定性摘要——压缩失败不应该让整个 Run 失败。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .._utils import clip, estimate_tokens
from ..cancel import CancelToken
from ..config import ContextConfig
from ..llm.base import ModelRequest, Provider
from ..llm.mock import deterministic_summary
from ..prompts import COMPACTION_PROMPT, COMPACTION_SYSTEM, COMPACTION_UPDATE_HINT
from ..runtime.session import ContextView, SessionLog, render_summary  # noqa: F401（render_summary 供外部复用）
from ..tools.extra import latest_todos
from ..types import Message, MsgKind, Usage, user_message


def message_tokens(m: Message) -> int:
    n = estimate_tokens(m.text) + estimate_tokens(m.thinking) + 4
    for c in m.tool_calls:
        n += estimate_tokens(c.name) + estimate_tokens(json.dumps(c.arguments, ensure_ascii=False)) + 8
    return n


def find_cut_index(messages: list[Message], keep_recent_tokens: int, start: int = 0) -> int | None:
    """返回保留段的起始下标 j（messages[:j] 被压缩）；无可压缩内容时返回 None。纯函数，便于测试。"""
    acc, i = 0, len(messages)
    while i > start:
        acc += message_tokens(messages[i - 1])
        if acc >= keep_recent_tokens:
            break
        i -= 1
    i = max(i - 1, start)
    if i <= start:
        return None

    def is_user_boundary(m: Message) -> bool:
        return m.role == "user" and m.kind not in (MsgKind.REMINDER, MsgKind.COMPACTION)

    # 优先：向后找用户消息边界；其次：向后找 assistant 边界；都不行就向前找
    for pred in (is_user_boundary, lambda m: m.role == "assistant"):
        for j in range(i, len(messages)):
            if pred(messages[j]):
                return j if j > start else None
    for j in range(i - 1, start, -1):
        if messages[j].role != "tool":
            return j
    return None


def serialize_transcript(messages: list[Message], tool_chars: int = 1500) -> str:
    """把消息序列化成给摘要模型看的纯文本（去掉 thinking，工具结果截断）。"""
    lines: list[str] = []
    for m in messages:
        if m.role == "user":
            tag = "[系统提醒]" if m.kind == MsgKind.REMINDER else "[用户]"
            lines.append(f"{tag}: {clip(m.text, 4000)}")
        elif m.role == "assistant":
            if m.text:
                lines.append(f"[助手]: {clip(m.text, 3000)}")
            for c in m.tool_calls:
                lines.append(f"[工具调用] {c.name}({clip(json.dumps(c.arguments, ensure_ascii=False), 400)})")
        else:
            mark = "✗" if m.is_error else "✓"
            lines.append(f"[工具结果 {m.tool_name} {mark}]: {clip(m.text, tool_chars)}")
    return "\n".join(lines)


def extract_file_ops(messages: list[Message]) -> tuple[set[str], set[str]]:
    read, modified = set(), set()
    for m in messages:
        for c in m.tool_calls:
            p = c.arguments.get("path")
            if not p:
                continue
            if c.name in ("read",):
                read.add(str(p))
            elif c.name in ("write", "edit"):
                modified.add(str(p))
    return read, modified


@dataclass
class CompactionResult:
    entry_id: str
    summary: str
    tokens_before: int
    tokens_after: int
    summarized: int
    first_kept_entry_id: str
    used_fallback: bool = False
    details: dict[str, Any] = field(default_factory=dict)
    usage: Usage | None = None   # 摘要调用本身的用量：同样要计入预算与成本


class Compactor:
    def __init__(self, provider: Provider, model: str, cfg: ContextConfig, max_summary_tokens: int = 2048):
        self.provider = provider
        self.model = model
        self.cfg = cfg
        self.max_summary_tokens = max_summary_tokens

    async def compact(
        self, log: SessionLog, view: ContextView, cancel: CancelToken, *, tokens_before: int,
        extra_instructions: str | None = None, run_id: str | None = None, force: bool = False,
        window: int | None = None,
    ) -> CompactionResult | None:
        msgs = view.messages
        start = 1 if view.compaction else 0
        keep = self.cfg.keep_recent_tokens
        if window:  # 小窗口模型按比例少保留一些原文，否则压缩后立刻又会超阈值
            keep = min(keep, int(window * 0.3))
        if force:
            keep = min(keep, 2000)
        cut = find_cut_index(msgs, keep, start)
        if cut is None:
            return None
        to_sum = msgs[start:cut]
        if not to_sum:
            return None
        prev = view.compaction.payload if view.compaction else {}
        transcript = serialize_transcript(to_sum)
        previous = COMPACTION_UPDATE_HINT.format(summary=prev["summary"]) if prev.get("summary") else ""
        prompt = COMPACTION_PROMPT.format(previous=previous, conversation=transcript)
        if extra_instructions:
            prompt += f"\n\n额外要求（来自 pre_compact hook）：\n{extra_instructions}"
        req = ModelRequest(model=self.model, system=COMPACTION_SYSTEM, messages=[user_message(prompt)],
                           tools=[], max_tokens=self.max_summary_tokens, purpose="compaction")
        used_fallback = False
        usage = None
        try:
            out = await self.provider.complete(req, cancel)
            usage = out.usage
            summary = out.text.strip()
            if not summary:
                raise ValueError("摘要为空")
        except Exception as e:  # 取消要向上传播；其它失败退化为确定性摘要
            from ..cancel import RunCancelled

            if isinstance(e, RunCancelled):
                raise
            summary, used_fallback = deterministic_summary(req), True

        read, modified = extract_file_ops(to_sum)
        read |= set(prev.get("read_files", []))
        modified |= set(prev.get("modified_files", []))
        # 原始任务原文由代码确定性地保留：取被压缩部分中最近一次用户发起的任务；没有则沿用上次摘要的记录
        prompts = [m.text for m in to_sum if m.role == "user" and m.kind == MsgKind.PROMPT]
        goal = prompts[-1] if prompts else prev.get("goal", "")
        first_kept = view.entry_ids[cut]
        # 压缩后的水位（估算）：摘要 + 保留段；随条目一起落盘，便于事后审计“压缩省了多少”
        tokens_after_est = estimate_tokens(summary) + 200 + sum(message_tokens(m) for m in msgs[cut:])
        payload = {
            "summary": summary,
            "goal": clip(goal, 2000),
            "first_kept_entry_id": first_kept,
            "tokens_before": tokens_before,
            "tokens_after": tokens_after_est,
            "read_files": sorted(read),
            "modified_files": sorted(modified),
            "summarized_messages": len(to_sum),
            "fallback": used_fallback,
            # 当前计划同样确定性保留：摘要之后模型仍能看到完整的 todo
            "todos": latest_todos(log.messages()) or prev.get("todos"),
        }
        entry = log.append("compaction", payload, run_id=run_id)
        after = log.context()
        tokens_after = sum(message_tokens(m) for m in after.messages)
        return CompactionResult(entry.id, summary, tokens_before, tokens_after, len(to_sum), first_kept, used_fallback,
                                {"read_files": sorted(read), "modified_files": sorted(modified)}, usage)

