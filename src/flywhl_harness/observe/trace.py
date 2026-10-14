"""轨迹（trajectory）与可观测性。

一条可回放的轨迹至少要记录：
- **输入**：用户 prompt、系统提示词（会话创建时冻结）、工具集与 schema、模型与参数；
- **每一步的非确定性结果**：模型输出（含工具调用）、工具结果、usage、时间；
- **控制面事件**：状态迁移、审批、取消、压缩、重试、降级；
- **上下文指纹**：每次模型调用时“模型看到了什么”的哈希——回放时用它定位分叉点。

``to_otel_spans`` 把事件流映射为 OpenTelemetry GenAI 语义约定风格的 span：
``invoke_agent``（一次 Run）→ ``chat``（一次模型调用）→ ``execute_tool``（一次工具调用），
属性使用 ``gen_ai.*`` 命名（provider、model、usage、tool.name、tool.call.id、conversation.id）。
消息内容属于敏感数据，默认不写入 span（与语义约定的 opt-in 原则一致）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..events import E, Event
from ..runtime.store import Store
from ..types import Message


def export_trajectory(store: Store, run_id: str, *, include_children: bool = True) -> dict[str, Any]:
    run = store.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    session = store.get_session(run.session_id)
    events = store.list_events(run_id)
    entries = [e for e in store.path(run.session_id) if e.run_id == run_id] or \
              [e for e in store.list_entries(run.session_id) if e.run_id == run_id]
    messages = [e.payload for e in entries if e.kind == "message"]
    # 指纹与回放时的模型调用一一对齐：只保留最终落盘为有效 assistant 消息的那些调用
    # （出错 / 被中止的调用在回放时不会重演；压缩调用走单独的确定性摘要）
    replayable = {m.get("id") for m in messages if m.get("role") == "assistant"
                  and m.get("stop_reason") not in ("error", "aborted")}
    fingerprints = [ev.data["fingerprint"] for ev in events
                    if ev.type == E.MESSAGE_START and ev.data.get("fingerprint")
                    and ev.data.get("purpose") != "compaction" and ev.data.get("message_id") in replayable]
    children = [export_trajectory(store, r.id) for r in store.child_runs(run_id)] if include_children else []
    return {
        "format": "flywhl-trajectory/1",
        "run": run.to_dict(),
        "session": {"id": session.id, "title": session.title, "workspace": session.workspace,
                    "system_prompt": session.meta.get("system_prompt"), "model": session.meta.get("model"),
                    "tools": session.meta.get("tools")} if session else None,
        "messages": messages,
        "compactions": [e.payload for e in entries if e.kind == "compaction"],
        "steps": [s.__dict__ for s in store.list_steps(run_id)],
        "events": [ev.to_dict() for ev in events if ev.type != E.MESSAGE_UPDATE],
        "fingerprints": fingerprints,
        "children": children,
        "spans": to_otel_spans(run.to_dict(), events, session.meta if session else {}),
    }


def _sid(*parts: str) -> str:
    return hashlib.sha1("/".join(parts).encode()).hexdigest()[:16]


def to_otel_spans(run: dict[str, Any], events: list[Event], session_meta: dict[str, Any]) -> list[dict[str, Any]]:
    trace_id = hashlib.sha1(run["id"].encode()).hexdigest()[:32]
    root_id = _sid(run["id"], "root")
    provider = session_meta.get("provider", "unknown")
    model = session_meta.get("model", "unknown")
    spans: list[dict[str, Any]] = []
    start = next((e.ts for e in events), run.get("created_at") or 0)
    end = next((e.ts for e in reversed(events)), start)
    end_ev = next((e for e in reversed(events) if e.type == E.RUN_END), None)
    usage = run.get("usage") or {}
    spans.append({
        "trace_id": trace_id, "span_id": root_id, "parent_span_id": None, "name": "invoke_agent flywhl",
        "start_ns": int(start * 1e9), "end_ns": int(end * 1e9),
        "attributes": {
            "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "flywhl", "gen_ai.agent.id": run["id"],
            "gen_ai.conversation.id": run["session_id"], "gen_ai.provider.name": provider, "gen_ai.request.model": model,
            "gen_ai.usage.input_tokens": usage.get("input_tokens", 0), "gen_ai.usage.output_tokens": usage.get("output_tokens", 0),
            "flywhl.run.status": end_ev.data.get("status") if end_ev else run.get("status"),
        },
    })
    open_chat: dict[str, Event] = {}
    open_tool: dict[str, Event] = {}
    for ev in events:
        if ev.type == E.MESSAGE_START:
            open_chat[ev.data["message_id"]] = ev
        elif ev.type == E.MESSAGE_END and ev.data.get("message", {}).get("role") == "assistant":
            m = ev.data["message"]
            st = open_chat.pop(m["id"], None)
            u = m.get("usage") or {}
            spans.append({
                "trace_id": trace_id, "span_id": _sid(run["id"], m["id"]), "parent_span_id": root_id,
                "name": f"chat {m.get('model') or model}", "start_ns": int((st.ts if st else ev.ts) * 1e9), "end_ns": int(ev.ts * 1e9),
                "attributes": {
                    "gen_ai.operation.name": "chat", "gen_ai.provider.name": m.get("provider") or provider,
                    "gen_ai.request.model": model, "gen_ai.response.model": m.get("model"),
                    "gen_ai.response.finish_reasons": [m.get("stop_reason")],
                    "gen_ai.usage.input_tokens": u.get("input_tokens", 0), "gen_ai.usage.output_tokens": u.get("output_tokens", 0),
                    "gen_ai.usage.cache_read.input_tokens": u.get("cache_read_tokens", 0),
                },
            })
        elif ev.type == E.TOOL_START:
            open_tool[ev.data["call_id"]] = ev
        elif ev.type == E.TOOL_END:
            st = open_tool.pop(ev.data["call_id"], None)
            attrs = {
                "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": ev.data.get("name"),
                "gen_ai.tool.call.id": ev.data["call_id"], "gen_ai.tool.type": "function",
                "flywhl.tool.status": ev.data.get("status"),
            }
            if ev.data.get("is_error"):
                attrs["error.type"] = ev.data.get("status") or "tool_error"
            spans.append({
                "trace_id": trace_id, "span_id": _sid(run["id"], ev.data["call_id"]), "parent_span_id": root_id,
                "name": f"execute_tool {ev.data.get('name')}", "start_ns": int((st.ts if st else ev.ts) * 1e9),
                "end_ns": int(ev.ts * 1e9), "attributes": attrs,
            })
    return spans


def trajectory_messages(traj: dict[str, Any]) -> list[Message]:
    return [Message.from_dict(m) for m in traj["messages"]]


def dumps(traj: dict[str, Any]) -> str:
    return json.dumps(traj, ensure_ascii=False, indent=1, default=str)
