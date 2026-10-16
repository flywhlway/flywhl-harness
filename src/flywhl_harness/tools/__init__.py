"""内置工具集。顺序即发送给模型的顺序——保持稳定，前缀缓存才能命中。"""

from __future__ import annotations

from pathlib import Path

from ..config import HarnessConfig
from .base import Effect, FunctionTool, Tool, ToolContext, ToolResult, TransientToolError, tool
from .demo import FakeNotifyService, SendNotificationTool, SleepTool
from .extra import ReadRefTool, SearchRefTool, TodoWriteTool
from .fs import EditTool, GrepTool, LsTool, ReadTool, WriteTool
from .registry import ToolRegistry
from .shell import BashTool


def default_tools(cfg: HarnessConfig, data_dir: Path | None = None) -> ToolRegistry:
    from ..agent.subagent import TaskTool

    reg = ToolRegistry([
        ReadTool(), WriteTool(), EditTool(),
        BashTool(cfg.permissions.strip_secret_env, cfg.tool_timeout, cfg.permissions.sandbox),
        LsTool(), GrepTool(), TodoWriteTool(), ReadRefTool(), SearchRefTool(), TaskTool(),
    ])
    if cfg.enable_demo_tools:
        dd = data_dir or cfg.data_path()
        reg.register(SendNotificationTool(FakeNotifyService(dd / "external_notify.db")))
        reg.register(SleepTool())
    return reg


__all__ = [
    "Tool", "ToolResult", "ToolContext", "Effect", "FunctionTool", "tool", "TransientToolError", "ToolRegistry",
    "ReadTool", "WriteTool", "EditTool", "LsTool", "GrepTool", "BashTool", "TodoWriteTool", "ReadRefTool",
    "SearchRefTool", "SendNotificationTool", "SleepTool", "FakeNotifyService", "default_tools",
]
