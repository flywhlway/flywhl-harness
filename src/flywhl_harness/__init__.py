"""flywhl-harness：从零手写的最小 Agent Harness。

    Agent = Model + Harness

模型负责“想”，harness 负责其余的一切：循环、工具、上下文、状态、生命周期、策略、执行环境与度量。

最小用法::

    import asyncio
    from flywhl_harness import Harness, HarnessConfig

    async def main():
        async with Harness(HarnessConfig.load()) as h:
            result = await h.run("列出目录")
            print(result.status, result.text)

    asyncio.run(main())
"""

from .agent.loop import AgentLoop, Outcome
from .cancel import CancelToken, RunCancelled, RunPaused
from .config import HarnessConfig
from .events import E, Event
from .llm import DemoProvider, ReplayProvider, ScriptedProvider, create_provider
from .policy.hooks import CommandHook, Hook, HookBus, HookEvent, HookInput, HookOutput
from .policy.permissions import Decision, Mode, PermissionPolicy, Rule
from .runtime.manager import Harness, RunResult
from .runtime.state import RunStatus
from .runtime.store import Store
from .tools import Effect, Tool, ToolContext, ToolRegistry, ToolResult, default_tools, tool
from .types import Message, ToolCall, Usage

__version__ = "0.1.0"

__all__ = [
    "Harness", "HarnessConfig", "RunResult", "RunStatus", "Store", "AgentLoop", "Outcome",
    "CancelToken", "RunCancelled", "RunPaused", "E", "Event",
    "ScriptedProvider", "DemoProvider", "ReplayProvider", "create_provider",
    "Hook", "HookBus", "HookEvent", "HookInput", "HookOutput", "CommandHook",
    "PermissionPolicy", "Rule", "Mode", "Decision",
    "Tool", "ToolResult", "ToolContext", "ToolRegistry", "Effect", "tool", "default_tools",
    "Message", "ToolCall", "Usage", "__version__",
]
