"""Hooks：把“必须发生的事”从提示词移到确定性代码。

提示词里写“修改后请运行测试”，模型可能照做也可能忘记；Stop hook 里跑测试，则一定会发生。
这就是 hooks 的价值——在生命周期关键点 **拦截、修改、审计、阻止**。

挂载点
------
==================  ========================================================
session_start        会话开始：注入环境信息（目录结构、可用命令）
user_prompt_submit   用户输入提交：敏感词拦截、补充上下文
pre_tool_use         工具执行前：allow/deny/ask、改写参数（注入租户 ID、规范化路径）
post_tool_use        工具执行后：审计落库、输出脱敏、编辑后自动格式化
stop                 模型想结束时：跑验收（测试/产物/清单），不满足则 block 并给出缺口
subagent_stop        子代理结束时：校验回传契约
pre_compact          压缩前：补充必须保留的信息
run_end              Run 结束：通知、指标上报
==================  ========================================================

失败策略：安全类 hook **fail-closed**（hook 自己出错也按阻断处理），观测类 hook **fail-open**。
执行顺序：按 priority 升序 **串行** 执行（便于参数改写链式传递；Claude Code 选择并行执行）；deny 短路。

进程外 hook（``CommandHook``）与 Claude Code 的协议兼容：stdin 传 JSON，退出码 0=放行（stdout 可返回 JSON 决策），
2=阻断（stderr 作为原因回给模型），其它=非阻断错误。
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import os
import signal
from dataclasses import dataclass, field, fields
from typing import Any, Awaitable, Callable

from .permissions import strictest


class HookEvent:
    SESSION_START = "session_start"
    USER_PROMPT_SUBMIT = "user_prompt_submit"
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    STOP = "stop"
    SUBAGENT_STOP = "subagent_stop"
    PRE_COMPACT = "pre_compact"
    RUN_END = "run_end"


@dataclass
class HookInput:
    event: str
    run_id: str
    session_id: str
    cwd: str = ""
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_result: dict[str, Any] | None = None   # {"content", "is_error", "details"}
    prompt: str | None = None
    stop_blocks: int = 0                         # Stop 已被阻止的次数：hook 自己也应防止无限续跑
    last_message: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    # 进程内 hook 可以直接访问运行时对象（如消息历史、工作区）；进程外 hook 只拿到 JSON
    context: Any = None

    def to_json(self) -> dict[str, Any]:
        # 注意不能用 dataclasses.asdict：它会深拷贝 context（运行时对象，含数据库连接）而失败
        d = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "context"}
        # 去掉空字段，但工具类事件的 tool_input 必须保留（无参工具的 {} 也是合法输入，hook 脚本常直接取它）
        d = {k: v for k, v in d.items() if v is not None and (v not in ({}, "") or k == "tool_input")}
        d["hook_event_name"] = d.pop("event")
        if "tool_result" in d:
            d["tool_response"] = d.pop("tool_result")
        return d


@dataclass
class HookOutput:
    decision: str | None = None          # pre_tool_use: allow|deny|ask；stop/post_tool_use: block
    reason: str = ""
    updated_input: dict[str, Any] | None = None
    updated_result: str | None = None    # post_tool_use：替换给模型看的结果（例如脱敏）
    additional_context: str | None = None
    stop_run: bool = False               # 立即终止整个 Run（Claude Code 的 continue:false）
    warnings: list[str] = field(default_factory=list)


HookFn = Callable[[HookInput], Awaitable[HookOutput | None]]

_PRE_TOOL_DECISIONS = {"allow": "allow", "approve": "allow", "ask": "ask", "deny": "deny", "block": "deny"}


def normalize_decision(event: str, decision: str | None) -> tuple[str | None, str | None]:
    """把各种写法统一成运行时认识的决策，返回 (决策, 警告)。

    - pre_tool_use 只认 allow / ask / deny；``block``（Claude Code 的旧写法）等同 deny，``approve`` 等同 allow；
    - 其它事件只有 ``block`` 有意义（deny 视同 block），allow/approve 等于“不干预”；
    - 不认识的取值 **不能静默忽略**：给出警告，安全类 hook 写错一个单词就失效是最危险的情况。
    """
    if not decision:
        return None, None
    d = str(decision).strip().lower()
    if event == HookEvent.PRE_TOOL_USE:
        if d in _PRE_TOOL_DECISIONS:
            return _PRE_TOOL_DECISIONS[d], None
    elif d in ("block", "deny"):
        return "block", None
    elif d in ("allow", "approve", "continue"):
        return None, None
    return None, f"无法识别的 hook 决策 {decision!r}（{event}），已忽略"


@dataclass
class Hook:
    event: str
    fn: HookFn
    name: str = ""
    matcher: str = "*"        # 工具名 glob（只对工具类事件生效），支持 "read|grep"
    priority: int = 100
    timeout: float = 10.0
    fail_closed: bool = False
    source: str = "user"      # managed | project | user

    def applies(self, inp: HookInput) -> bool:
        if inp.tool_name is None or self.matcher in ("*", ""):
            return True
        return any(fnmatch.fnmatchcase(inp.tool_name, m.strip()) for m in self.matcher.split("|"))


class HookBus:
    def __init__(self) -> None:
        self._hooks: list[Hook] = []

    def add(self, hook: Hook) -> Hook:
        if not hook.name:
            hook.name = getattr(hook.fn, "__name__", hook.event)
        self._hooks.append(hook)
        return hook

    def on(self, event: str, matcher: str = "*", *, priority: int = 100, timeout: float = 10.0,
           fail_closed: bool = False, source: str = "user") -> Callable[[HookFn], HookFn]:
        """装饰器注册进程内 hook::

            @hooks.on("pre_tool_use", matcher="bash")
            async def guard(inp): ...
        """

        def deco(fn: HookFn) -> HookFn:
            self.add(Hook(event, fn, fn.__name__, matcher, priority, timeout, fail_closed, source))
            return fn

        return deco

    def has(self, event: str) -> bool:
        return any(h.event == event for h in self._hooks)

    async def run(self, event: str, inp: HookInput) -> HookOutput:
        """按优先级串行执行匹配的 hook，并聚合结果。"""
        agg = HookOutput()
        reasons: list[str] = []
        contexts: list[str] = []
        for h in sorted((h for h in self._hooks if h.event == event and h.applies(inp)), key=lambda h: h.priority):
            try:
                out = await asyncio.wait_for(h.fn(inp), h.timeout)
            except Exception as e:  # 含超时
                msg = f"hook {h.name} 失败：{type(e).__name__}: {e}"
                if h.fail_closed:
                    block = "deny" if event == HookEvent.PRE_TOOL_USE else "block"
                    return HookOutput(decision=block, reason=msg + "（fail-closed，按阻断处理）")
                agg.warnings.append(msg + "（fail-open，忽略）")
                continue
            if out is None:
                continue
            if out.updated_input is not None:
                inp.tool_input = out.updated_input   # 链式传递给下一个 hook
                agg.updated_input = out.updated_input
            if out.updated_result is not None:
                agg.updated_result = out.updated_result
                if inp.tool_result is not None:
                    inp.tool_result = {**inp.tool_result, "content": out.updated_result}
            if out.additional_context:
                contexts.append(out.additional_context)
            decision, warn = normalize_decision(event, out.decision)
            if warn:
                agg.warnings.append(f"[{h.name}] {warn}")
            if decision:
                agg.decision = strictest(agg.decision, decision) if decision != "block" else "block"
                if out.reason:
                    reasons.append(f"[{h.name}] {out.reason}")
            if out.stop_run:
                agg.stop_run = True
                reasons.append(f"[{h.name}] {out.reason or '要求终止运行'}")
            if agg.decision in ("deny", "block") and event == HookEvent.PRE_TOOL_USE:
                break  # deny 短路
        agg.reason = "\n".join(reasons)
        agg.additional_context = "\n".join(contexts) or None
        return agg


class CommandHook:
    """进程外 hook：执行 shell 命令，与 Claude Code 的 hook 协议兼容。

    可识别的 stdout JSON 字段（均可选）::

        {"decision": "block", "reason": "...",               # stop / post_tool_use 阻断
         "continue": false, "stopReason": "...",             # 终止整个运行
         "hookSpecificOutput": {"permissionDecision": "allow|deny|ask",
                                "permissionDecisionReason": "...",
                                "updatedInput": {...}, "additionalContext": "..."}}
    """

    def __init__(self, command: str, cwd: str | None = None):
        self.command = command
        self.cwd = cwd
        self.__name__ = f"command:{command.split()[0] if command.split() else command}"

    async def __call__(self, inp: HookInput) -> HookOutput | None:
        proc = await asyncio.create_subprocess_shell(
            self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=self.cwd or inp.cwd or None,
            env={**os.environ, "FLYWHL_HOOK_EVENT": inp.event}, start_new_session=True,
        )
        try:
            out, err = await proc.communicate(json.dumps(inp.to_json(), ensure_ascii=False).encode("utf-8"))
        except BaseException:
            # HookBus 的超时会取消这里：必须杀掉整个进程组，否则超时的 hook 脚本（及其子进程）会在后台继续运行
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 2)
            raise
        stdout, stderr = out.decode("utf-8", "replace").strip(), err.decode("utf-8", "replace").strip()
        if proc.returncode == 2:
            return HookOutput(decision="deny" if inp.event == HookEvent.PRE_TOOL_USE else "block",
                              reason=stderr or "被命令 hook 阻断")
        if proc.returncode != 0:
            raise RuntimeError(f"退出码 {proc.returncode}：{stderr[:300]}")
        if not stdout.startswith("{"):
            return HookOutput(additional_context=stdout) if stdout and inp.event in (
                HookEvent.SESSION_START, HookEvent.USER_PROMPT_SUBMIT) else None
        data = json.loads(stdout)
        spec = data.get("hookSpecificOutput") or {}
        res = HookOutput(
            decision=spec.get("permissionDecision") or data.get("decision"),
            reason=spec.get("permissionDecisionReason") or data.get("reason") or data.get("stopReason") or "",
            updated_input=spec.get("updatedInput"),
            updated_result=spec.get("updatedToolOutput"),
            additional_context=spec.get("additionalContext") or data.get("additionalContext"),
            stop_run=data.get("continue") is False,
        )
        return res  # 决策取值由 HookBus 统一归一化（approve → allow、block → deny ……）
