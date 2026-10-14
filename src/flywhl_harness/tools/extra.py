"""辅助工具：todo_write（外置计划）、read_ref / search_ref（读取外置句柄）。

todo_write 的意义不在“记账”，而在 **上下文工程**：
- 计划被写成结构化状态而不是散落在对话里，压缩时不会丢；
- harness 会把当前 todo 以 system-reminder 的形式复述到上下文末尾（Manus 的 todo.md 复述技巧），
  把全局目标反复推到模型注意力最集中的位置，缓解长任务中的“目标漂移”；
- Stop 钩子可以检查“是否还有未完成项”，作为验收条件之一。

todo 的状态不单独存储：它就是最后一次 todo_write 调用的参数——从消息历史即可重建（事件溯源思想）。
"""

from __future__ import annotations

import re
from typing import Any

from .base import Effect, Tool, ToolContext, ToolResult

TODO_STATUSES = ["pending", "in_progress", "completed", "cancelled"]
_ICON = {"pending": "☐", "in_progress": "◐", "completed": "☑", "cancelled": "☒"}


def render_todos(todos: list[dict[str, Any]]) -> str:
    return "\n".join(f"{_ICON.get(t.get('status', 'pending'), '☐')} {t.get('content', '')}" for t in todos)


class TodoWriteTool(Tool):
    name = "todo_write"
    description = (
        "维护当前任务的计划清单（整体覆盖写入）。多步骤任务开始时创建清单，执行中及时更新状态："
        "同一时间只应有一项 in_progress；完成一项立即标记 completed。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "事项描述"},
                        "status": {"type": "string", "enum": TODO_STATUSES},
                    },
                    "required": ["content", "status"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["todos"],
        "additionalProperties": False,
    }
    effect = Effect.READ  # 只改 harness 内部状态，不触碰外部世界

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        todos = args["todos"]
        ctx.state["todos"] = todos
        counts = {s: sum(1 for t in todos if t["status"] == s) for s in TODO_STATUSES}
        warn = ""
        if counts["in_progress"] > 1:
            warn = "\n注意：同时有多项 in_progress，建议一次只推进一项。"
        return ToolResult(
            content=(
                f"计划已更新：共 {len(todos)} 项，完成 {counts['completed']}，进行中 {counts['in_progress']}，"
                f"待办 {counts['pending']}。{warn}"
            ),
            details={"todos": todos},
        )


class ReadRefTool(Tool):
    name = "read_ref"
    description = "按行读取被外置的大结果（句柄形如 blob_xxxx，出现在被截断的工具输出中）。"
    parameters = {
        "type": "object",
        "properties": {
            "ref": {"type": "string", "description": "句柄，如 blob_1a2b3c4d5e6f7a8b"},
            "offset": {"type": "integer", "minimum": 1, "default": 1, "description": "起始行"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200, "description": "行数"},
        },
        "required": ["ref"],
        "additionalProperties": False,
    }
    effect = Effect.READ

    def permission_target(self, args: dict[str, Any]) -> str:
        return str(args.get("ref", ""))

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        data = ctx.blobs.get(ctx.session_id, args["ref"])
        if data is None:
            return ToolResult.error(f"句柄 {args['ref']} 不存在、已过期或不属于当前会话。")
        lines = data.splitlines()
        off, lim = args.get("offset", 1), args.get("limit", 200)
        if off > len(lines):
            return ToolResult.error(f"offset={off} 超出范围：该句柄共 {len(lines)} 行。")
        chunk = lines[off - 1 : off - 1 + lim]
        end = off + len(chunk) - 1
        more = f"\n[第 {off}–{end} 行，共 {len(lines)} 行；继续请用 offset={end + 1}]" if end < len(lines) else ""
        return ToolResult(content="\n".join(chunk) + more, details={"ref": args["ref"], "total_lines": len(lines)})


class SearchRefTool(Tool):
    name = "search_ref"
    description = "在外置的大结果中按正则搜索，返回匹配行及行号（适合在巨大日志/输出中定位）。"
    parameters = {
        "type": "object",
        "properties": {
            "ref": {"type": "string"},
            "pattern": {"type": "string", "description": "正则表达式"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
        },
        "required": ["ref", "pattern"],
        "additionalProperties": False,
    }
    effect = Effect.READ

    def permission_target(self, args: dict[str, Any]) -> str:
        return str(args.get("ref", ""))

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        data = ctx.blobs.get(ctx.session_id, args["ref"])
        if data is None:
            return ToolResult.error(f"句柄 {args['ref']} 不存在、已过期或不属于当前会话。")
        try:
            rx = re.compile(args["pattern"])
        except re.error as e:
            return ToolResult.error(f"正则无效：{e}")
        hits = [f"{i}: {ln[:300]}" for i, ln in enumerate(data.splitlines(), 1) if rx.search(ln)]
        cap = args.get("max_results", 50)
        more = f"\n[共 {len(hits)} 处匹配，仅显示前 {cap} 处]" if len(hits) > cap else ""
        return ToolResult(content="\n".join(hits[:cap]) + more if hits else "未找到匹配。", details={"count": len(hits)})


def latest_todos(messages: list[Any]) -> list[dict[str, Any]] | None:
    """从消息历史重建 todo：取最后一次成功的 todo_write 调用参数。"""
    ok_ids = {m.tool_call_id for m in messages if m.role == "tool" and m.tool_name == "todo_write" and not m.is_error}
    for m in reversed(messages):
        if m.role == "assistant":
            for c in reversed(m.tool_calls):
                if c.name == "todo_write" and c.id in ok_ids:
                    return c.arguments.get("todos")
    return None
