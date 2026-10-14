"""上下文管理：每一轮在调用模型之前，决定“模型看到什么”。

从轻到重的一组手段
------------------
=================  =======================================================  ===============
手段                做法                                                      触发
=================  =======================================================  ===============
入口截断与外置       超大工具结果写入 BlobStore，上下文只放预览 + 句柄             结果超过阈值
旧结果清理           较早轮次的大工具结果替换为“已清理，句柄 X”的占位             水位超过清理阈值
外置记忆            todo、进度文件、NOTES：状态在文件里而不在上下文里             工具驱动
计划复述            把未完成的 todo 以 system-reminder 复述到上下文末尾          每隔若干轮
子代理隔离           高噪声探索交给子代理，只回传结论                            模型决定
摘要压缩            见 compaction.py                                         水位超过压缩阈值
=================  =======================================================  ===============

水位估算借鉴 pi：**最近一次 assistant 的真实 usage（输入侧）+ 其后新增消息的估算值**，
比全量估算准确得多（真实 usage 已经包含了 system 与 tools 的开销）。

关于前缀稳定：清理旧结果会改写历史，打碎前缀缓存；对要求“只追加”的模型（Claude 新模型的 thinking 签名）
还会导致请求被拒。所以清理用一个只前进的 **水位线**（watermark）实现——只有在水位线移动的那一刻前缀才变化，
并且对 ``append_only`` 的模型直接关闭清理，交给压缩处理。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .._utils import estimate_tokens, head_tail_lines
from ..config import ContextConfig
from ..llm.models import ModelInfo
from ..runtime.store import BlobStore
from ..tools.base import ToolResult
from ..tools.extra import latest_todos, render_todos
from ..types import Message, MsgKind, TextBlock, ToolCall
from .compaction import message_tokens

_REMINDER_TAG = re.compile(r"<\s*/?\s*system-reminder\b[^>]*>", re.I)  # 含大小写变体与带属性的写法


@dataclass
class ContextStats:
    tokens: int
    window: int
    compact_at: int
    clear_at: int
    source: str            # usage+estimate | estimate

    @property
    def ratio(self) -> float:
        return self.tokens / self.window if self.window else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"tokens": self.tokens, "window": self.window, "compact_at": self.compact_at,
                "clear_at": self.clear_at, "ratio": round(self.ratio, 4), "source": self.source}


class ContextManager:
    def __init__(self, cfg: ContextConfig, model: ModelInfo):
        self.cfg = cfg
        self.model = model
        self.window = cfg.context_window or model.context_window

    # ------------------------------------------------------------------ 水位
    def thresholds(self) -> tuple[int, int]:
        compact_at = int(min(self.window * self.cfg.compact_threshold, self.window - self.cfg.reserve_tokens))
        clear_at = int(self.window * self.cfg.clear_threshold)
        return max(compact_at, 1), max(clear_at, 1)

    def estimate(self, messages: list[Message], system: str, tools: list[dict]) -> ContextStats:
        compact_at, clear_at = self.thresholds()
        # 找到最后一条带真实 usage 的 assistant。注意：压缩后保留段里的消息是在压缩 **之前** 生成的，
        # 它们的 usage 反映的是旧的（更大的）上下文，必须忽略——只信任压缩之后产生的 usage。
        comp_ts = next((m.ts for m in messages if m.kind == MsgKind.COMPACTION), None)
        last = None
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            if comp_ts is not None and m.ts <= comp_ts:
                break
            if m.role == "assistant" and m.usage and m.usage.context_tokens > 0:
                last = i
                break
        if last is not None:
            base = messages[last].usage.context_tokens + messages[last].usage.output_tokens  # type: ignore[union-attr]
            tail = sum(message_tokens(m) for m in messages[last + 1 :])
            return ContextStats(base + tail, self.window, compact_at, clear_at, "usage+estimate")
        total = estimate_tokens(system) + estimate_tokens(json.dumps(tools, ensure_ascii=False))
        total += sum(message_tokens(m) for m in messages)
        return ContextStats(total, self.window, compact_at, clear_at, "estimate")

    def should_compact(self, stats: ContextStats) -> bool:
        return self.cfg.auto_compact and stats.tokens > stats.compact_at

    # ------------------------------------------------------------------ 入口：工具结果外置
    def shape_tool_result(self, result: ToolResult, blobs: BlobStore, scope: str) -> ToolResult:
        """超大结果外置：上下文只放头尾预览 + 句柄 + 读取提示。截断必须显式告知模型。

        同时转义结果中的 ``<system-reminder>`` 标签：这个标签是 harness 与模型之间的“信道”，
        网页、文件、MCP 返回的内容里若出现它，就是在冒充 harness 下指令（提示注入的常见手法）。
        """
        if "system-reminder" in result.content.lower():
            result = ToolResult(_REMINDER_TAG.sub(lambda m: m.group(0).replace("<", "&lt;").replace(">", "&gt;"),
                                                  result.content), result.is_error, result.details, result.tainted)
        limit = self.cfg.tool_result_max_tokens
        if estimate_tokens(result.content) <= limit:
            return result
        # 工具自己已经外置过完整输出（如 bash）：复用同一个句柄，不重复存储
        ref = (result.details or {}).get("full_output_ref") or blobs.put(scope, result.content)
        preview, total = head_tail_lines(result.content, head=60, tail=20, max_chars=limit * 3)
        text = (
            f"[结果过大（约 {estimate_tokens(result.content)} tokens，共 {total} 行），已外置为句柄 {ref}。"
            f"以下为头尾预览；如需更多内容，请用 read_ref(ref, offset, limit) 或 search_ref(ref, pattern) 按需读取]\n"
            + preview
        )
        details = dict(result.details or {})
        details["externalized_ref"] = ref
        return ToolResult(text, result.is_error, details, result.tainted)

    # ------------------------------------------------------------------ 清理：旧结果占位
    def apply_clearing(self, messages: list[Message], stats: ContextStats, watermark: int) -> tuple[list[Message], int]:
        """把水位线（消息下标）之前的大工具结果替换为占位。返回 (新消息列表, 新水位线)。

        水位线只在水位超过清理阈值时前进到“最近 N 轮之前”，因此相邻两次请求的前缀大多数时候完全一致。
        """
        if not self.cfg.clear_old_tool_results or self.model.append_only:
            return messages, watermark
        if stats.tokens > stats.clear_at:
            turns = [i for i, m in enumerate(messages) if m.role == "assistant"]
            keep = self.cfg.keep_tool_results_turns
            if len(turns) > keep:
                watermark = max(watermark, turns[-keep])
        if watermark <= 0:
            return messages, watermark
        out = []
        for i, m in enumerate(messages):
            if i < watermark and m.role == "assistant" and any(_big_args(c.arguments) for c in m.tool_calls):
                out.append(_clear_args(m))
                continue
            if i < watermark and m.role == "tool" and estimate_tokens(m.text) > 400:
                ref = (m.details or {}).get("externalized_ref") or (m.details or {}).get("full_output_ref")
                hint = f"，句柄 {ref}（可用 read_ref 重新读取）" if ref else "，如需请重新调用工具获取"
                out.append(Message(role="tool", content=[TextBlock(f"[较早的工具结果已清理以节省上下文：原文约 {estimate_tokens(m.text)} tokens{hint}]")],
                                   id=m.id, ts=m.ts, tool_call_id=m.tool_call_id, tool_name=m.tool_name,
                                   is_error=m.is_error, meta=m.meta))
            else:
                out.append(m)
        return out, watermark

    # ------------------------------------------------------------------ 复述：计划提醒
    @staticmethod
    def todo_reminder(messages: list[Message], every_turns: int = 4) -> str | None:
        """距离上一次 todo_write（或上一次复述）超过 N 轮、且有未完成项时，返回复述文本。"""
        todos = latest_todos(messages)
        if not todos or all(t.get("status") in ("completed", "cancelled") for t in todos):
            return None
        since = 0
        for m in reversed(messages):
            if m.role == "assistant":
                if any(c.name == "todo_write" for c in m.tool_calls):
                    break
                since += 1
            if m.role == "user" and m.kind == MsgKind.REMINDER and "当前计划" in m.text:
                break
        if since < every_turns:
            return None
        return "当前计划（请对照推进，完成一项就及时用 todo_write 更新状态）：\n" + render_todos(todos)


def _big_args(args: dict[str, Any]) -> bool:
    return any(isinstance(v, str) and len(v) > 2000 for v in args.values())


def _clear_args(m: Message) -> Message:
    """把旧 assistant 消息里超长的工具参数（如 write 的文件全文）替换为占位：文件已在磁盘上，需要时重新 read。"""
    content = []
    for c in m.content:
        if isinstance(c, ToolCall) and _big_args(c.arguments):
            args = {k: (f"[已清理：原参数 {len(v)} 字符]" if isinstance(v, str) and len(v) > 2000 else v)
                    for k, v in c.arguments.items()}
            content.append(ToolCall(c.id, c.name, args))
        else:
            content.append(c)
    return Message(role="assistant", content=content, id=m.id, ts=m.ts, stop_reason=m.stop_reason, usage=m.usage,
                   model=m.model, provider=m.provider, meta=m.meta)
