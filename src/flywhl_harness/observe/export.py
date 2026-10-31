"""会话导出为 Markdown：给人读的对话记录（评审、归档、贴进 issue），不是可回放的轨迹（那是 ``trace.py``）。

- 只导出当前分支（``store.path``），与控制台时间线看到的一致；压缩点保留摘要。
- 工具调用写成“参数 + 结果”两个代码块；结果过长时截断保尾（错误与结论通常在最后）。
- 代码块的围栏比内容里最长的反引号串还长：工具输出里的 ``` 不会提前结束代码块，后面的内容也不会被当成 Markdown 渲染。
- harness 注入给模型的消息（提醒、预算收尾指令……）默认不导出：它们是控制面，不是对话。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from ..runtime.store import Store

RESULT_CHARS = 4000
_INJECTED = {"reminder": "系统提醒", "resume_note": "恢复说明", "stop_feedback": "验收反馈", "wrap_up": "预算收尾指令",
             "compaction": "压缩摘要"}
_USER_TAG = {"steer": "（插话）", "follow_up": "（排队）"}


def _fence(text: str, lang: str = "") -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{text.rstrip()}\n{fence}"


def _blocks(m: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [b for b in m.get("content") or [] if b.get("type") == kind]


def _ts(t: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "—"


def _clip_tail(text: str, limit: int = RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return f"[共 {len(text)} 字，只保留最后 {limit} 字]\n…" + text[-limit:]


def session_markdown(store: Store, session_id: str, *, include_injected: bool = False) -> str:
    s = store.get_session(session_id)
    if s is None:
        raise KeyError(session_id)
    entries = store.path(session_id)
    results = {e.payload.get("tool_call_id"): e.payload for e in entries
               if e.kind == "message" and e.payload.get("role") == "tool"}
    runs = store.list_runs(session_id, limit=500)
    cost = sum((r.usage or {}).get("cost_usd") or 0 for r in runs)
    out = [f"# {s.title}", "",
           f"> 会话 `{s.id}` · 创建于 {_ts(s.created_at)} · 最近更新 {_ts(s.updated_at)} · {len(runs)} 次运行"
           + (f" · 费用约 ${cost:.4f}" if cost else ""), ""]
    for e in entries:
        p = e.payload
        if e.kind == "compaction":
            out += ["---", "", f"*上下文在这里压缩过：{p.get('tokens_before', '?')} → {p.get('tokens_after', '?')} tokens。摘要：*", "",
                    _fence(str(p.get("summary") or "")), ""]
            continue
        if e.kind != "message":
            continue
        role, kind = p.get("role"), (p.get("meta") or {}).get("kind") or "prompt"
        if role == "user":
            text = "".join(b.get("text", "") for b in p.get("content") or [])
            if kind in _INJECTED:
                if include_injected:
                    out += [f"<details><summary>{_INJECTED[kind]}（harness 注入）</summary>", "", _fence(text), "", "</details>", ""]
                continue
            out += [f"## 用户{_USER_TAG.get(kind, '')}", "", text.strip(), ""]
        elif role == "assistant":
            text = "".join(b.get("text", "") for b in _blocks(p, "text")).strip()
            calls = _blocks(p, "tool_call")
            if not text and not calls:
                continue
            out += ["## 助手" + (f"（{p['model']}）" if p.get("model") else ""), ""]
            if text:
                out += [text, ""]
            for c in calls:
                r = results.get(c.get("id"))
                status = "未执行" if r is None else "失败" if r.get("is_error") else "完成"
                out += [f"**工具 `{c.get('name')}`** · {status}", "",
                        _fence(json.dumps(c.get("arguments") or {}, ensure_ascii=False, indent=2), "json"), ""]
                if r is not None:
                    body = "".join(b.get("text", "") for b in r.get("content") or [])
                    out += [_fence(_clip_tail(body) or "(无输出)"), ""]
            if p.get("stop_reason") == "error":
                out += [f"> 模型调用失败：{p.get('error') or '未知错误'}", ""]
    return "\n".join(out).rstrip() + "\n"
