"""内置工具集。顺序即发送给模型的顺序——保持稳定，前缀缓存才能命中。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..config import HarnessConfig
from .base import Effect, FunctionTool, Tool, ToolContext, ToolResult, TransientToolError, tool
from .demo import FakeNotifyService, SendNotificationTool, SleepTool
from .extra import ReadRefTool, SearchRefTool, TodoWriteTool
from .fs import EditTool, GrepTool, LsTool, ReadTool, WriteTool
from .registry import ToolRegistry
from .shell import BashTool

if TYPE_CHECKING:
    from ..net.search import WebService


def default_tools(cfg: HarnessConfig, data_dir: Path | None = None, *, web: "WebService | None" = None) -> ToolRegistry:
    """``web`` 是共用的联网服务（熔断、并发、缓存都在它身上）；不给且 ``[web].enabled`` 时新建一个。
    联网工具排在 task 之后：没开联网的会话钉住的工具名单与不启用联网时逐项相同。"""
    from ..agent.subagent import TaskTool

    bash = BashTool(cfg.permissions.strip_secret_env, cfg.tool_timeout, cfg.permissions.sandbox,
                    cfg.permissions.sandbox_network, cfg.permissions.network_allow,
                    cfg.permissions.unix_sockets, [str(data_dir or cfg.data_path())])
    reg = ToolRegistry([
        ReadTool(), WriteTool(bash.confinement), EditTool(bash.confinement), bash,
        LsTool(), GrepTool(), TodoWriteTool(), ReadRefTool(), SearchRefTool(), TaskTool(),
    ])
    if cfg.web.enabled:
        from ..net.search import WebService
        from .web import web_tools

        for t in web_tools(web or WebService(cfg.web), cfg.web):
            reg.register(t)
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
