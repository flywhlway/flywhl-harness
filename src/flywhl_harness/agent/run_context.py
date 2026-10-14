"""RunContext：一次 Run 执行期间 Loop 与执行器共享的全部依赖与可变状态。

把依赖显式集中在一个对象里（而不是散落在全局变量中）有两个好处：
1. 测试时可以替换任何一个零件（mock 模型、内存存储、自定义权限策略）；
2. 子代理只需“派生”一个新的 RunContext（更窄的工具集、切出的预算、子取消令牌、全新的会话），
   就能复用同一套 Loop。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from ..cancel import CancelToken
    from ..config import HarnessConfig
    from ..llm.base import Provider
    from ..llm.models import ModelInfo
    from ..policy.approvals import ApprovalBroker
    from ..policy.hooks import HookBus
    from ..policy.permissions import PermissionPolicy, Rule
    from ..runtime.session import SessionLog
    from ..runtime.store import Store
    from ..runtime.workspace import Workspace
    from ..tools.registry import ToolRegistry
    from ..types import Usage
    from .budget import Budget, LoopGuard
    from .compaction import Compactor
    from .context import ContextManager
    from .inbox import Inbox


@dataclass
class RunContext:
    run_id: str
    session_id: str
    log: "SessionLog"
    store: "Store"
    workspace: "Workspace"
    provider: "Provider"
    model: str
    model_info: "ModelInfo"
    tools: "ToolRegistry"
    config: "HarnessConfig"
    permissions: "PermissionPolicy"
    hooks: "HookBus"
    approvals: "ApprovalBroker"
    context: "ContextManager"
    compactor: "Compactor"
    budget: "Budget"
    loop_guard: "LoopGuard"
    cancel: "CancelToken"
    inbox: "Inbox"
    system_prompt: str
    # 状态迁移回调（running ↔ waiting_human），由运行时注入
    set_status: Callable[[str, str], Awaitable[None]] = field(default=None)  # type: ignore[assignment]
    grants: list["Rule"] = field(default_factory=list)      # 会话级授权（“本会话总是允许”）
    state: dict[str, Any] = field(default_factory=dict)     # 工具间共享状态（文件读取时间戳等）
    harness: Any = None
    blob_scope: str = ""                                    # 外置句柄的隔离范围（子代理共享根会话的范围）
    role: str = "main"                                      # main | subagent
    depth: int = 0
    resume: bool = False
    resume_note: str | None = None
    # ---- 运行中可变的控制量 ----
    pause_requested: bool = False
    stop_requested: str | None = None                       # hook 要求终止（continue:false）
    tainted: bool = False                                   # 读取过不可信内容
    # 预算可以跨 Run 共享（子代理的多次尝试共用一份子预算）：本 Run 自己的用量 = 当前预算用量 - 起点
    usage_base: Usage | None = None
    stop_blocks: int = 0
    clear_watermark: int = 0
    pending_reminders: list[str] = field(default_factory=list)
    # 子代理把事件透传给父 Run 的出口（父 Run 的执行器会把它接到自己的事件队列上）
    forward: Callable[[str, dict[str, Any]], None] | None = None

    @property
    def run_usage(self) -> Usage:
        """本 Run 的用量。主 Run 恢复时预算从检查点还原，起点为空：用量跨暂停 / 崩溃累计。"""
        return self.budget.usage - self.usage_base if self.usage_base is not None else self.budget.usage

    @property
    def scope(self) -> str:
        return self.blob_scope or self.session_id

    def remind(self, text: str) -> None:
        """登记一条将在下一次模型调用前注入的 system-reminder。"""
        if text and text not in self.pending_reminders:
            self.pending_reminders.append(text)
