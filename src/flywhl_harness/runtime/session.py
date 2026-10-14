"""会话日志：一棵只追加的条目树（借鉴 pi 的 JSONL 会话树）。

- 每个条目有 ``id`` 与 ``parent_id``；session.head 指向当前分支的叶子；
- **分支（branch）**：把 head 移到某个早先的条目，之后追加的条目自然形成新分支，旧分支原样保留；
- **压缩（compaction）**：追加一个 compaction 条目，记录摘要与“从哪条开始保留原文”（first_kept_entry_id），
  构建上下文时用摘要替换它之前的历史——原始历史仍在库里，可回查、可回放。

上下文视图 = ``[摘要] + path[first_kept : compaction) + path(compaction : head]``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..types import Message, MsgKind, TextBlock, ThinkingBlock
from .store import Entry, Store


@dataclass
class ContextView:
    messages: list[Message]
    compaction: Entry | None
    entry_ids: list[str]        # 与 messages 一一对应的条目 ID（摘要消息为 compaction 条目 ID）


def render_summary(payload: dict[str, Any]) -> str:
    """把 compaction 条目渲染为给模型看的摘要正文：模型摘要 + 确定性提取的原始任务与文件清单。"""
    parts = [payload.get("summary", "")]
    if payload.get("goal"):
        parts.append("## 原始任务（原文）\n" + payload["goal"])
    if payload.get("todos"):
        marks = {"pending": "☐", "in_progress": "◐", "completed": "☑", "cancelled": "☒"}
        parts.append("## 当前计划\n" + "\n".join(f"{marks.get(t.get('status'), '☐')} {t.get('content', '')}"
                                                 for t in payload["todos"]))
    if payload.get("read_files"):
        parts.append("<read-files>\n" + "\n".join(payload["read_files"]) + "\n</read-files>")
    if payload.get("modified_files"):
        parts.append("<modified-files>\n" + "\n".join(payload["modified_files"]) + "\n</modified-files>")
    return "\n\n".join(p for p in parts if p)


class SessionLog:
    def __init__(self, store: Store, session_id: str):
        self.store = store
        self.session_id = session_id

    # ------------------------------------------------------------------ 写
    def append_message(self, msg: Message, run_id: str | None = None) -> Entry:
        return self.store.append_entry(self.session_id, "message", msg.to_dict(), run_id=run_id, entry_id=f"e_{msg.id}")

    def append(self, kind: str, payload: dict[str, Any], run_id: str | None = None) -> Entry:
        return self.store.append_entry(self.session_id, kind, payload, run_id=run_id)

    def branch(self, entry_id: str | None) -> None:
        """把当前分支头移到 entry_id（None 表示回到会话开头）。"""
        if entry_id is not None:
            e = self.store.get_entry(entry_id)
            if e is None or e.session_id != self.session_id:
                raise KeyError(f"条目不存在或不属于当前会话：{entry_id}")
        self.store.update_session(self.session_id, head=entry_id)

    # ------------------------------------------------------------------ 读
    def path(self) -> list[Entry]:
        return self.store.path(self.session_id)

    def messages(self) -> list[Message]:
        """当前分支上的全部原始消息（不考虑压缩）。"""
        return [Message.from_dict(e.payload) for e in self.path() if e.kind == "message"]

    def context(self) -> ContextView:
        """模型应当看到的上下文（考虑最近一次压缩）。"""
        path = self.path()
        idx = next((i for i in range(len(path) - 1, -1, -1) if path[i].kind == "compaction"), None)
        if idx is None:
            msgs = [(e.id, Message.from_dict(e.payload)) for e in path if e.kind == "message"]
            return ContextView([m for _, m in msgs], None, [i for i, _ in msgs])
        comp = path[idx]
        first_kept = comp.payload.get("first_kept_entry_id")
        start = next((i for i, e in enumerate(path) if e.id == first_kept), idx)
        summary = Message(
            role="user",
            content=[TextBlock(
                "<summary>\n以下是此前对话的压缩摘要（原始历史已归档）。请基于它继续工作，不要重复已完成的步骤。\n\n"
                + render_summary(comp.payload) + "\n</summary>"
            )],
            id=f"sum_{comp.id}",
            ts=comp.created_at,
            meta={"kind": MsgKind.COMPACTION, "entry_id": comp.id},
        )
        out, ids = [summary], [comp.id]
        for e in path[start:idx]:
            if e.kind == "message":
                m = Message.from_dict(e.payload)
                # 保留段的 thinking 是在“完整历史”下生成的，摘要替换历史后其签名会失效：只保留文本与工具调用
                m.content = [c for c in m.content if not isinstance(c, ThinkingBlock)]
                out.append(m)
                ids.append(e.id)
        for e in path[idx + 1 :]:
            if e.kind == "message":
                out.append(Message.from_dict(e.payload))
                ids.append(e.id)
        return ContextView(out, comp, ids)

    def tree(self) -> list[dict[str, Any]]:
        """整棵会话树（供 UI 展示分支）。"""
        s = self.store.get_session(self.session_id)
        on_path = {e.id for e in self.path()}
        rows = []
        for e in self.store.list_entries(self.session_id):
            p = e.payload
            label = ""
            if e.kind == "message":
                label = (p.get("role", "") + ": " + "".join(
                    b.get("text", "") for b in p.get("content", []) if b.get("type") == "text"))[:80]
                calls = [b.get("name") for b in p.get("content", []) if b.get("type") == "tool_call"]
                if calls:
                    label += f" [{', '.join(calls)}]"
            elif e.kind == "compaction":
                label = f"压缩摘要（压缩前约 {p.get('tokens_before', '?')} tokens）"
            rows.append({"id": e.id, "parent_id": e.parent_id, "kind": e.kind, "label": label,
                         "role": p.get("role"), "kind_meta": (p.get("meta") or {}).get("kind"),
                         "on_path": e.id in on_path, "head": s is not None and s.head == e.id, "run_id": e.run_id})
        return rows
