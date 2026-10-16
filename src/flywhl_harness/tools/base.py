"""工具（ACI，Agent-Computer Interface）的核心抽象。

工具是“给模型用的界面”。一个好的工具定义需要回答五个问题：

=================  =======================================================
叫什么、干什么     ``name`` / ``description``：动词_名词，描述写清单位、格式、示例
参数长什么样       ``parameters``：JSON Schema；用 enum、必填、范围做防呆
副作用多大         ``effect``：决定并发、重试、审批、取消四种策略（见 Effect）
能不能中途打断     ``interruptible``：不可逆操作不能半途打断
结果给谁看         ``ToolResult.content`` 给模型，``details`` 给 UI
=================  =======================================================

副作用分级是整个策略体系的“主键”：

=========  ===========================  ======  ======  ========  ===============
Effect     示例                         并发    自动重试  默认审批   取消时
=========  ===========================  ======  ======  ========  ===============
READ       读文件、搜索、查询            可并发   可       放行       立即中止
WRITE      写工作区文件（可快照回滚）     串行    带幂等键  按模式     中止并可回滚
EXTERNAL   发通知、下发指令、改生产数据   串行     否      必须确认   等待完成或标记未知
=========  ===========================  ======  ======  ========  ===============
"""

from __future__ import annotations

import functools
import inspect
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .schema import schema_from_signature

if TYPE_CHECKING:  # 仅用于类型标注，避免循环导入
    from ..cancel import CancelToken
    from ..runtime.store import BlobStore
    from ..runtime.workspace import Workspace


class Effect(StrEnum):
    READ = "read"
    WRITE = "write"
    EXTERNAL = "external"


EFFECT_RANK = {Effect.READ: 0, Effect.WRITE: 1, Effect.EXTERNAL: 2}


class TransientToolError(Exception):
    """工具的瞬时错误（网络抖动、锁冲突）。只对只读或幂等工具自动重试。"""


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    details: dict[str, Any] | None = None
    # 结果来自不可信来源（网页、邮件、工单……）：用于“污点感知”，提升后续副作用操作的审批等级
    tainted: bool = False

    @classmethod
    def error(cls, message: str, **details: Any) -> "ToolResult":
        return cls(content=message, is_error=True, details=details or None)


@dataclass
class ToolContext:
    """执行工具时 harness 提供的运行环境。工具只通过它接触外界，便于测试与替换。"""

    run_id: str
    session_id: str
    call_id: str
    workspace: "Workspace"
    cancel: "CancelToken"
    blobs: "BlobStore"
    # 幂等键：run_id + call_id，跨崩溃重启稳定不变；写操作把它传给外部系统去重
    idempotency_key: str
    # 推送进度/部分输出（tool_update 事件），给 UI 实时展示
    emit: Callable[[dict[str, Any]], None] = lambda _u: None
    # Run 级共享状态（如文件读取时间戳），工具之间可通过它协作
    state: dict[str, Any] = field(default_factory=dict)
    # 对 harness 运行时的引用（子代理工具需要用它派生子 Run）
    harness: Any = None
    run: Any = None
    # 是否处于崩溃恢复的对账流程中
    recovering: bool = False
    # 距离 Run 截止时间的剩余秒数（None 表示没有墙钟限额）：自管超时的工具据此收紧自己的超时
    time_left: float | None = None


@functools.cache
def _takes_workspace(fn: Callable[..., Any]) -> bool:
    try:
        return "workspace" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


class Tool:
    """工具基类。子类实现 ``execute``；有外部副作用的工具建议同时实现 ``reconcile``。"""

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    effect: Effect = Effect.READ
    interruptible: bool = True       # 取消时能否直接中止
    idempotent: bool = False         # 用同一幂等键重复执行是否安全
    timeout: float | None = None     # None = 使用全局 tool_timeout
    untrusted_output: bool = False   # 输出是否天然不可信（污点来源）
    # 工具自己管理超时（如 bash：超时后要杀进程组并保留部分输出）。执行器只在其之外留一点余量兜底，
    # 否则执行器的计时器先触发，会把“部分输出 + 已终止进程组”这样有用的结果吞掉
    manages_timeout: bool = False
    cancel_grace: float | None = None  # 不可中断时的取消宽限期；None = 使用全局 cancel_grace_seconds

    # ------------------------------------------------------------------ 必须实现
    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise NotImplementedError

    # ------------------------------------------------------------------ 可选覆盖
    async def reconcile(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult | None:
        """对账：执行结果未知（有 intent 无 result）时，凭幂等键向外部系统查询真实状态。

        返回 None 表示查不到（外部系统没有这笔记录），harness 会据此决定重试还是上报。
        """
        return None

    def interruptible_for(self, args: dict[str, Any], workspace: str | None = None) -> bool:
        """按参数判定能否在取消时直接中止。默认取静态 interruptible；bash 对外部操作（如 git push）返回 False。"""
        return self.interruptible

    def effect_for(self, args: dict[str, Any], workspace: str | None = None) -> Effect:
        """动态副作用等级。默认取静态 ``effect``；bash 这类工具会按命令内容动态判定。

        ``workspace`` 是工作区根目录：命令里指向工作区内部的绝对路径按内部路径算。执行器与权限层都要传它，
        否则 ``cat /ws/app.py`` 在权限层是只读（plan 模式放行），执行器却记成 external，界面、预写意图与恢复各说各话。
        """
        return self.effect

    # 执行器、权限层、回放只通过这两个入口取等级：早期的扩展示例按 effect_for(self, args) 覆盖，
    # 直接把 workspace 传进去会抛 TypeError。旧写法照常工作，只是拿不到工作区
    def effect_in(self, args: dict[str, Any], workspace: str | None) -> Effect:
        if _takes_workspace(type(self).effect_for):
            return self.effect_for(args, workspace)
        return self.effect_for(args)

    def interruptible_in(self, args: dict[str, Any], workspace: str | None) -> bool:
        if _takes_workspace(type(self).interruptible_for):
            return self.interruptible_for(args, workspace)
        return self.interruptible_for(args)

    def untrusted_for(self, args: dict[str, Any]) -> bool:
        """这次调用的输出是否来自不可信来源（污点）。默认取静态 untrusted_output；bash 下载网页时返回 True。"""
        return self.untrusted_output

    def preview(self, args: dict[str, Any], ctx: "ToolContext") -> str | None:
        """审批前的变更预览（例如 diff）：让审批人看到“会改成什么样”，而不只是一行摘要。"""
        return None

    def permission_target(self, args: dict[str, Any]) -> str:
        """权限规则匹配的目标字符串，例如 bash 的命令、文件工具的路径。"""
        for key in ("path", "command", "url", "to"):
            if key in args:
                return str(args[key])
        return ""

    def describe_call(self, args: dict[str, Any]) -> str:
        """人类可读的调用摘要，用于审批弹窗和 UI 卡片标题。"""
        target = self.permission_target(args)
        return f"{self.name}({target})" if target else self.name

    # ------------------------------------------------------------------ 规格
    @property
    def concurrency_safe(self) -> bool:
        return self.effect == Effect.READ

    def spec(self) -> dict[str, Any]:
        """发送给模型的工具规格（provider 适配层会再转换成各家格式）。"""
        return {"name": self.name, "description": self.description, "parameters": self.parameters}

    def __repr__(self) -> str:
        return f"<Tool {self.name} effect={self.effect}>"


class FunctionTool(Tool):
    """把一个 async 函数包装成工具（由 ``@tool`` 装饰器创建）。"""

    def __init__(self, fn: Callable[..., Awaitable[Any]], **attrs: Any):
        self.fn = fn
        self.name = attrs.pop("name", None) or fn.__name__
        self.description = attrs.pop("description", None) or inspect.getdoc(fn) or ""
        self.parameters = attrs.pop("parameters", None) or schema_from_signature(fn)
        self._wants_ctx = "ctx" in inspect.signature(fn).parameters
        self._reconcile_fn = attrs.pop("reconcile", None)
        for k, v in attrs.items():
            setattr(self, k, v)

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        out = await (self.fn(**args, ctx=ctx) if self._wants_ctx else self.fn(**args))
        return out if isinstance(out, ToolResult) else ToolResult(content="" if out is None else str(out))

    async def reconcile(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult | None:
        if self._reconcile_fn is None:
            return None
        return await self._reconcile_fn(args, ctx)


def tool(
    fn: Callable[..., Awaitable[Any]] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    effect: Effect = Effect.READ,
    interruptible: bool = True,
    idempotent: bool = False,
    timeout: float | None = None,
    parameters: dict[str, Any] | None = None,
    reconcile: Callable[[dict, ToolContext], Awaitable[ToolResult | None]] | None = None,
) -> Any:
    """装饰器：用最少的代码定义一个工具。

    示例::

        @tool(effect=Effect.READ)
        async def get_weather(city: Annotated[str, "城市名，如 上海"]) -> str:
            \"\"\"查询城市当前天气。\"\"\"
            return "晴，23℃"
    """

    def wrap(f: Callable[..., Awaitable[Any]]) -> FunctionTool:
        return FunctionTool(
            f,
            name=name,
            description=description,
            parameters=parameters,
            effect=effect,
            interruptible=interruptible,
            idempotent=idempotent,
            timeout=timeout,
            reconcile=reconcile,
        )

    return wrap(fn) if fn is not None else wrap
