"""Harness 运行时：管理会话与 Run 的完整生命周期。

“大脑 / 双手 / 会话”三分（Anthropic Managed Agents 的架构思想）
------------------------------------------------------------
- **会话（Session）**：只追加的条目日志，存在于上下文窗口之外，是唯一的事实源；
- **大脑（Harness + 模型）**：本身无状态——进程崩溃后，新进程从存储中“唤醒”会话即可继续（``recover``）；
- **双手（工具 / 沙箱）**：通过统一的工具接口调用，可以本地执行，也可以换成远程沙箱。

这个模块负责
------------
1. **驱动 Loop**：把 AgentLoop 产出的事件交给 EventHub（分配 seq → 落库 → 扇出给订阅者）；
2. **Run 与连接解耦**：Run 在后台任务中执行，与 HTTP/SSE 连接无关；客户端断线重连只是重新订阅；
3. **生命周期控制**：取消（协作式）、暂停（安全点生效）、恢复（重建上下文 + 漂移说明）、插话、审批；
4. **崩溃恢复**：心跳 + 租约；心跳过期的 Run 经 CAS 标记为 interrupted，再从检查点恢复——
   未配对的工具调用先按幂等键对账，绝不盲目重放副作用；
5. **子代理**：派生隔离的子会话 / 子 Run，事件向父 Run 透传，回传结构化契约。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from .._utils import clip, now
from ..agent.budget import Budget, LoopGuard
from ..agent.compaction import Compactor
from ..agent.context import ContextManager
from ..agent.inbox import Inbox
from ..agent.loop import AgentLoop
from ..agent.run_context import RunContext
from ..agent.subagent import check_contract
from ..cancel import CancelToken
from ..config import BudgetConfig, HarnessConfig
from ..events import E, EPHEMERAL_EVENTS, Event
from ..llm import create_provider
from ..llm.base import Provider
from ..policy.approvals import ApprovalBroker, ApprovalDecision, Resolver
from ..policy.hooks import CommandHook, Hook, HookBus, HookEvent, HookInput
from ..policy.permissions import PermissionPolicy, Rule
from ..prompts import SUBAGENT_PROMPT, build_system_prompt
from ..tools import default_tools
from ..tools.registry import ToolRegistry
from ..types import unpaired_tool_calls
from .session import SessionLog
from .state import ACTIVE, IllegalTransition, RunStateMachine, RunStatus, TERMINAL, TransitionConflict
from .store import RunRecord, SessionRecord, Store
from .workspace import Workspace

_CLOSED = object()   # 枢纽关闭：Run 结束
_LAGGED = object()   # 订阅者太慢被踢下线：需要从数据库补齐后重新订阅


class EventHub:
    """单个 Run 的事件枢纽：分配单调递增的 seq、批量落库、扇出给订阅者。

    写放大治理：高频增量事件（文本 delta）先进缓冲，每 50ms 或攒够 64 条批量写入；其它事件立即写入。
    **先落库、再扇出**：订阅者只会收到已经持久化的事件。这样进程崩溃后 seq 不会被复用（客户端见过的
    seq 一定在库里），也满足“事件永远不领先于持久化状态”；代价是文本增量最多延迟 50ms。
    慢订阅者的队列满了就把它踢下线——客户端凭 Last-Event-ID 重连，从数据库补齐。
    """

    def __init__(self, store: Store, run_id: str, start_seq: int = 0, queue_size: int = 2000):
        self.store = store
        self.run_id = run_id
        self.seq = start_seq
        self.queue_size = queue_size
        self._buffer: list[Event] = []
        self._subs: list[asyncio.Queue] = []
        self._flusher: asyncio.Task | None = None
        self.closed = False
        self.fenced = False   # 租约已丢失：之后的事件一律丢弃（序号空间已归新主人）

    def fence(self) -> None:
        self.fenced = True
        self._buffer.clear()

    def publish(self, ev: Event) -> Event:
        self.seq += 1
        ev.seq, ev.run_id = self.seq, self.run_id
        self._buffer.append(ev)
        if ev.type not in EPHEMERAL_EVENTS or len(self._buffer) >= 64:
            self.flush()
        elif self._flusher is None or self._flusher.done():
            self._flusher = asyncio.get_running_loop().create_task(self._delayed_flush())
        return ev

    def _fanout(self, batch: list[Event]) -> None:
        for q in list(self._subs):
            for ev in batch:
                try:
                    q.put_nowait(ev)
                except asyncio.QueueFull:  # 背压：慢消费者的积压直接丢弃，改为通知它“从库里补齐”
                    self._subs.remove(q)
                    while not q.empty():
                        q.get_nowait()
                    q.put_nowait(_LAGGED)
                    break

    async def _delayed_flush(self) -> None:
        await asyncio.sleep(0.05)
        self.flush()

    def flush(self) -> None:
        if self.fenced:
            self._buffer.clear()
            return
        if self._buffer:
            batch, self._buffer = self._buffer, []
            self.store.append_events(self.run_id, batch)
            self._fanout(batch)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(self.queue_size)
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subs:
            self._subs.remove(q)

    def close(self) -> None:
        self.flush()
        self.closed = True
        for q in self._subs:
            if q.full():  # 订阅者收到 _CLOSED 后会从库里补齐尾巴，所以清空积压不会丢事件
                while not q.empty():
                    q.get_nowait()
            q.put_nowait(_CLOSED)
        self._subs.clear()


@dataclass
class ActiveRun:
    run_id: str
    session_id: str
    rc: RunContext
    loop: AgentLoop
    hub: EventHub
    task: asyncio.Task | None = None
    wrote: bool = False
    cp: dict[str, Any] = field(default_factory=dict)   # 运行中的检查点（预算、工作区清单、排队消息）
    finishing: bool = False   # 已进入收尾：之后状态离开活动集合是自己造成的，不是租约丢失
    fenced: bool = False      # 租约已丢失：本 Worker 不能再写入这个 Run 的任何状态


@dataclass
class RunResult:
    run_id: str
    session_id: str
    status: str
    reason: str
    text: str
    usage: dict[str, Any]
    events: list[Event] = field(default_factory=list)


class Harness:
    """flywhl-harness 的门面：创建会话、启动/控制 Run、订阅事件、崩溃恢复。"""

    def __init__(
        self, config: HarnessConfig | None = None, *, provider: Provider | None = None, model: str | None = None,
        tools: ToolRegistry | None = None, store: Store | None = None, hooks: HookBus | None = None,
        approval_resolver: Resolver | None = None, worker_id: str | None = None,
    ):
        self.config = config or HarnessConfig.load()
        self.workspace_root = Path(self.config.workspace).expanduser().resolve()
        self.data_dir = self.config.data_path()
        self.store = store or Store(self.data_dir / "harness.db")
        if provider is None:
            provider, model = create_provider(self.config)
        self.provider = provider
        self.model = model or self.config.model or "flywhl-demo"
        self.model_info = provider.model_info(self.model)
        self.tools = tools if tools is not None else default_tools(self.config, self.data_dir)
        self.hooks = hooks or HookBus()
        for hc in self.config.hooks:
            self.hooks.add(Hook(hc.event, CommandHook(hc.command, str(self.workspace_root)), name=f"cmd:{hc.command[:30]}",
                                matcher=hc.matcher, timeout=hc.timeout, fail_closed=hc.fail_closed, source="project"))
        self.approvals = ApprovalBroker(self.store, on_ask=self.config.permissions.on_ask,
                                        timeout=self.config.permissions.approval_timeout, resolver=approval_resolver)
        self.state = RunStateMachine(self.store)
        self.worker_id = worker_id or f"w{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._active: dict[str, ActiveRun] = {}
        self._workspaces: dict[str, Workspace] = {}
        self._mcp: list[Any] = []

    # ================================================================== 生命周期
    async def start(self) -> "Harness":
        """异步初始化：连接 MCP 服务器、把它们的工具注册进来。可重复调用（只初始化一次）。"""
        if getattr(self, "_started", False):
            return self
        self._started = True
        if self.config.mcp_servers:
            from ..tools.mcp import load_mcp_tools

            clients, mcp_tools = await load_mcp_tools(self.config.mcp_servers, cwd=str(self.workspace_root))
            self._mcp.extend(clients)
            for t in mcp_tools:
                self.tools.register(t, replace=True)
        return self

    async def aclose(self, grace: float = 10.0) -> None:
        """优雅关闭：活动 Run 在下一个安全点 **暂停**（而不是取消），重启后由 recover() 自动继续；
        宽限期内停不下来的（例如正在跑一个很长的工具）直接放弃——它们会在下次启动时被当作崩溃接管。"""
        for a in list(self._active.values()):
            if a.rc.role == "main":
                a.rc.state["paused_by"] = "shutdown"
                a.rc.pause_requested = True
        tasks = [a.task for a in self._active.values() if a.task is not None]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=grace)
            for t in pending:
                t.cancel()
            with contextlib.suppress(Exception):
                await asyncio.gather(*pending, return_exceptions=True)
        for c in self._mcp:
            with contextlib.suppress(Exception):
                await c.close()
        await self.provider.aclose()

    async def __aenter__(self) -> "Harness":
        return await self.start()

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ================================================================== 会话
    def create_session(self, title: str = "新会话", *, workspace: str | None = None, mode: str | None = None,
                       parent_session_id: str | None = None, system_prompt: str | None = None) -> SessionRecord:
        ws = Path(workspace).expanduser().resolve() if workspace else self.workspace_root
        cfg = self.config
        sp = system_prompt or build_system_prompt(ws, override=cfg.system_prompt, append=cfg.append_system_prompt,
                                                  agents_md=cfg.load_agents_md, skills=cfg.load_skills)
        meta = {
            # 钉住：系统提示与工具集在会话创建时冻结（版本钉住 + 前缀稳定）；
            # 模型以当前 Harness 为准（这里记录创建时的模型，实际服务的模型记录在每条 assistant 消息上）
            "system_prompt": sp, "model": self.model, "provider": self.provider.name, "tools": self.tools.names(),
            "mode": mode or cfg.permissions.mode, "grants": [], "clear_watermark": 0,
        }
        return self.store.create_session(title, str(ws), parent_session_id=parent_session_id, meta=meta)

    def workspace(self, session: SessionRecord) -> Workspace:
        if session.workspace not in self._workspaces:
            self._workspaces[session.workspace] = Workspace(session.workspace, self.data_dir, snapshots=self.config.snapshots)
        return self._workspaces[session.workspace]

    def log(self, session_id: str) -> SessionLog:
        return SessionLog(self.store, session_id)

    def set_mode(self, session_id: str, mode: str) -> None:
        s = self._session(session_id)
        PermissionPolicy(mode)  # 校验
        self.store.update_session(session_id, meta={**s.meta, "mode": mode})

    def _session(self, session_id: str) -> SessionRecord:
        s = self.store.get_session(session_id)
        if s is None:
            raise KeyError(f"会话不存在：{session_id}")
        return s

    # ================================================================== Run：启动
    def active_run_of(self, session_id: str) -> str | None:
        return next((rid for rid, a in self._active.items() if a.session_id == session_id), None)

    async def start_run(self, session_id: str, prompt: str) -> RunRecord:
        s = self._session(session_id)
        if (busy := self.active_run_of(session_id)) is not None:
            raise RuntimeError(f"会话中已有运行中的任务 {busy}：请用 steer / follow_up 追加输入，或等待其结束")
        warnings: list[str] = []
        if not self.store.path(session_id) and self.hooks.has(HookEvent.SESSION_START):
            out = await self.hooks.run(HookEvent.SESSION_START, HookInput(
                event=HookEvent.SESSION_START, run_id="", session_id=session_id, cwd=s.workspace))
            warnings = out.warnings
            if out.additional_context:
                prompt = f"{prompt}\n\n<system-reminder>\n{out.additional_context}\n</system-reminder>"
        run = self.store.create_run(session_id, prompt, config={"model": self.model, "mode": s.meta.get("mode")})
        self._offline_transition(run.id, RunStatus.QUEUED, "已创建，等待执行")
        self._hook_warnings(run.id, warnings)
        self._note_model_switch(run.id, s)
        self._launch(run, s, prompt=prompt)
        return self.store.get_run(run.id)  # type: ignore[return-value]

    def _hook_warnings(self, run_id: str, warnings: list[str]) -> None:
        """没有活动事件枢纽时（Run 开始前、结束后）的 hook 警告：同样记成 notice，不能悄悄吞掉。"""
        for w in warnings:
            self.store.append_event_next(run_id, E.NOTICE, {"level": "warn", "code": "hook_warning", "text": w})

    def _note_model_switch(self, run_id: str, s: SessionRecord) -> None:
        """会话记录了创建时的模型；换了模型继续老会话是允许的（例如升级），但必须显式记录：
        行为基线、缓存前缀、费用都会变化，回放与评测时要能看出“这里换过模型”。"""
        old = s.meta.get("model")
        if old and old != self.model:
            self.store.append_event_next(run_id, E.NOTICE, {"level": "warn", "code": "model_switched",
                                                            "text": f"会话模型由 {old} 切换为 {self.model}"})
            s.meta["model"] = self.model
            self.store.update_session(s.id, meta=s.meta)

    def _build_rc(self, run: RunRecord, s: SessionRecord, *, cancel: CancelToken, inbox: Inbox, resume: bool = False,
                  resume_note: str | None = None, role: str = "main", depth: int = 0, tools: ToolRegistry | None = None,
                  budget: Budget | None = None, blob_scope: str = "") -> RunContext:
        cfg = self.config
        ws = self.workspace(s)
        policy = PermissionPolicy(s.meta.get("mode", cfg.permissions.mode), cfg.permissions.rules, workspace=ws)
        grants = [Rule.parse(g, "session") for g in s.meta.get("grants", [])]
        ctxm = ContextManager(cfg.context, self.model_info)
        if tools is None:
            # 工具集在会话创建时钉住：之后 Harness 新增的工具不会出现在老会话里（前缀稳定），
            # 已不存在的工具自然被跳过
            pinned = s.meta.get("tools")
            tools = self.tools.subset(pinned) if pinned else self.tools
        if budget is None:
            budget = Budget(cfg.budget)
            if resume:  # 预算跨暂停 / 崩溃延续：从检查点继续计数，而不是从零开始
                budget.restore((run.checkpoint or {}).get("budget"))
        rc = RunContext(
            run_id=run.id, session_id=s.id, log=SessionLog(self.store, s.id), store=self.store, workspace=ws,
            provider=self.provider, model=self.model, model_info=self.model_info, tools=tools, config=cfg,
            permissions=policy, hooks=self.hooks, approvals=self.approvals, context=ctxm,
            compactor=Compactor(self.provider, self.model, cfg.context), budget=budget,
            loop_guard=LoopGuard(cfg.loop_guard), cancel=cancel, inbox=inbox,
            system_prompt=s.meta.get("system_prompt") or build_system_prompt(ws.root), grants=grants,
            harness=self, role=role, depth=depth, resume=resume, resume_note=resume_note,
            clear_watermark=int(s.meta.get("clear_watermark", 0)), blob_scope=blob_scope,
            tainted=bool(s.meta.get("tainted")),
        )
        return rc

    def _launch(self, run: RunRecord, s: SessionRecord, *, prompt: str | None, resume: bool = False,
                resume_note: str | None = None) -> ActiveRun:
        inbox = Inbox()
        carried = (run.checkpoint or {}).get("inbox") if resume else None
        for text in (carried or {}).get("steer", []):
            inbox.steer(text)       # 暂停前排队、尚未注入的消息随恢复一起带回
        for text in (carried or {}).get("follow_up", []):
            inbox.follow_up(text)
        rc = self._build_rc(run, s, cancel=CancelToken(), inbox=inbox, resume=resume, resume_note=resume_note)
        a = ActiveRun(run.id, s.id, rc, AgentLoop(rc), EventHub(self.store, run.id, self.store.max_event_seq(run.id)),
                      cp={k: v for k, v in (run.checkpoint or {}).items() if k != "inbox"})
        rc.set_status = lambda to, reason: self._set_status(a, to, reason)
        self._active[run.id] = a
        a.task = asyncio.create_task(self._drive(a, prompt), name=f"run:{run.id}")
        return a

    # ================================================================== Run：驱动
    async def _drive(self, a: ActiveRun, prompt: str | None) -> None:
        rc = a.rc
        try:
            # 与状态迁移同一个 CAS 认领租约：之后心跳若发现 worker_id 变了，说明已被别的 Worker 接管
            self._transition(a, RunStatus.RUNNING, "开始执行", expect={RunStatus.QUEUED},
                             worker_id=self.worker_id, heartbeat_at=now())
        except (IllegalTransition, TransitionConflict):
            # 认领失败：别的 Worker 抢先接管了它。安静退出——绝不能把别人正在跑的 Run 标成 failed
            a.hub.close()
            self._active.pop(a.run_id, None)
            return
        hb = asyncio.create_task(self._heartbeat(a))
        try:
            wrote = False
            async for ev in a.loop.run(prompt):
                a.hub.publish(ev)
                if ev.type == E.TOOL_START and ev.data.get("effect") != "read":
                    wrote = True
                elif ev.type == E.TURN_END:
                    # 每轮记录预算状态；有写操作的回合再记录工作区清单（恢复时据此检测“暂停期间的漂移”）
                    self._checkpoint(a, manifest=wrote)
                    wrote = False
            await self._finish(a)
        except Exception as e:  # pragma: no cover - 运行时自身的故障
            a.hub.publish(Event(E.ERROR, {"error": f"{type(e).__name__}: {e}"}))
            with contextlib.suppress(Exception):
                self._transition(a, RunStatus.FAILED, f"运行时异常：{e}")
        finally:
            hb.cancel()
            a.hub.close()
            self._active.pop(a.run_id, None)
            if self.hooks.has(HookEvent.RUN_END) and not a.fenced:
                with contextlib.suppress(Exception):
                    out = await self.hooks.run(HookEvent.RUN_END, HookInput(event=HookEvent.RUN_END, run_id=a.run_id,
                                                                            session_id=a.session_id, context=rc))
                    self._hook_warnings(a.run_id, out.warnings)  # 事件枢纽已关闭：直接追加

    async def _finish(self, a: ActiveRun) -> None:
        a.finishing = True
        out = a.loop.outcome
        assert out is not None
        rc = a.rc
        pending = rc.inbox.pending()
        if pending["steer"] or pending["follow_up"]:
            if out.status == "paused":
                a.cp["inbox"] = pending  # 恢复时带回
            else:  # 不能静默丢弃用户的输入：明确告知哪些排队消息没有被处理
                a.hub.publish(Event(E.NOTICE, {"level": "warn", "code": "inbox_dropped",
                                               "text": "以下排队消息未被处理：" + "；".join(pending["steer"] + pending["follow_up"])}))
        result = {"text": out.final_text, "reason": out.reason, "turns": rc.budget.turns, "by": out.by}
        self._checkpoint(a, paused=out.status == "paused")
        self.store.update_run(a.run_id, usage=rc.run_usage.to_dict(), result=result, error=out.error)
        cur = RunStatus(self.store.get_run(a.run_id).status)  # type: ignore[union-attr]
        target = RunStatus(out.status)
        if cur == RunStatus.CANCELLING and target != RunStatus.CANCELLED:
            # 取消请求已先一步通过 CAS 占位：即使 Loop 恰好完成，最终状态也以取消为准（先到者胜出）
            target, out.reason = RunStatus.CANCELLED, f"取消请求先于完成到达（Loop 结果：{out.status}）"
        if target == RunStatus.CANCELLED:
            if cur in (RunStatus.RUNNING, RunStatus.WAITING_HUMAN):
                self._transition(a, RunStatus.CANCELLING, out.reason)
            self._transition(a, RunStatus.CANCELLED, f"{out.reason}（发起人：{out.by}）")
        elif target == RunStatus.PAUSED:
            self._transition(a, RunStatus.PAUSED, out.reason)
        else:
            if cur == RunStatus.WAITING_HUMAN:
                self._transition(a, RunStatus.RUNNING, "继续")
            self._transition(a, target, out.reason)

    def _transition(self, a: ActiveRun, to: RunStatus, reason: str, expect: set[RunStatus] | None = None,
                    **fields: Any) -> None:
        before, after = self.state.transition(a.run_id, to, expect=expect, reason=reason, **fields)
        a.hub.publish(Event(E.STATUS, {"from": before.status, "to": after.status, "reason": reason, "version": after.version}))

    def _offline_transition(self, run_id: str, to: RunStatus, reason: str, **kw: Any) -> RunRecord:
        """不在活动 EventHub 中的迁移（创建、恢复、离线取消……）：同样写一条 status 事件，保证“每次迁移都有记录”。"""
        before, after = self.state.transition(run_id, to, reason=reason, **kw)
        self.store.append_event_next(run_id, E.STATUS, {"from": before.status, "to": after.status, "reason": reason,
                                                         "version": after.version})
        return after

    async def _set_status(self, a: ActiveRun, to: str, reason: str) -> None:
        cur = self.store.get_run(a.run_id)
        if cur is None or cur.status == to:
            return
        with contextlib.suppress(Exception):  # 取消可能已经抢先迁移到 cancelling：以先到者为准
            self._transition(a, RunStatus(to), reason)

    async def _heartbeat(self, a: ActiveRun) -> None:
        while True:
            if a.finishing:
                return
            cur = self.store.get_run(a.run_id)
            if cur is not None and (cur.status not in ACTIVE and cur.status != RunStatus.QUEUED
                                    or (cur.worker_id and cur.worker_id != self.worker_id)):
                # 租约已丢失（例如本进程假死期间被别的 Worker 接管）：自我 fencing
                return self._fence(a, "租约丢失：Run 已被其它 Worker 接管")
            if not self.store.heartbeat(a.run_id, self.worker_id):
                return self._fence(a, "租约丢失：续租失败")
            await asyncio.sleep(self.config.heartbeat_seconds)

    def _fence(self, a: ActiveRun, why: str) -> None:
        """失去租约 = 对这个 Run 而言本进程已经“崩溃”：立即中止驱动任务、丢弃之后的一切写入。

        不能走协作式取消——那条路会照常收尾（迁移到 cancelled、写 usage 与检查点、补“已取消”的工具结果），
        而这个 Run 此刻已经属于新主人，旧 Worker 的收尾会把它改坏。新主人会按“结果未知”对账在途的调用。
        """
        if a.finishing:
            return
        a.fenced = True
        a.hub.fence()
        if a.task is not None and not a.task.done():
            a.task.cancel()
        else:  # 子 Run 在父 Run 的任务里执行：只能协作式地停下（子 Run 由本 Worker 独占，收尾不会碰到别人）
            a.rc.cancel.cancel(why, by="system:fencing")

    def _checkpoint(self, a: ActiveRun, *, manifest: bool = False, paused: bool = False) -> None:
        cp = a.cp
        cp["at"] = now()
        cp["budget"] = a.rc.budget.state()
        if manifest or paused or "manifest" not in cp:
            cp["manifest"] = a.rc.workspace.manifest()
        if paused:
            cp["paused_at"] = now()
            cp["paused_by"] = a.rc.state.get("paused_by", "user")
        self.store.update_run(a.run_id, checkpoint=cp)

    # ================================================================== Run：控制
    async def cancel(self, run_id: str, reason: str = "用户取消", by: str = "user") -> bool:
        """协作式取消：先 CAS 迁移到 cancelling（让“取消”与“完成”的竞争有唯一赢家），再发出取消令牌。"""
        a = self._active.get(run_id)
        if a is not None:
            cur = self.store.get_run(run_id)
            if cur is not None and cur.status in (RunStatus.RUNNING, RunStatus.WAITING_HUMAN):
                try:
                    self._transition(a, RunStatus.CANCELLING, f"{reason}（发起人：{by}）")
                except Exception:
                    return False  # 刚好完成了：完成者胜出
            a.rc.cancel.cancel(reason, by)
            return True
        run = self.store.get_run(run_id)
        if run is None or RunStatus(run.status) in TERMINAL:
            return False
        if run.status in (RunStatus.PAUSED, RunStatus.QUEUED, RunStatus.CREATED, RunStatus.INTERRUPTED):
            self._pair_dangling_offline(run, f"已取消：{reason}")
            self._offline_transition(run_id, RunStatus.CANCELLED, f"{reason}（发起人：{by}）")
            return True
        return False

    async def pause(self, run_id: str) -> bool:
        a = self._active.get(run_id)
        if a is None:
            return False
        a.rc.pause_requested = True  # 在下一个安全点生效：在途工具会先执行完
        a.hub.publish(Event(E.NOTICE, {"level": "info", "code": "pause_requested", "text": "已请求暂停，将在下一个安全点生效"}))
        return True

    async def resume(self, run_id: str) -> RunRecord:
        """恢复 paused / interrupted 的 Run：重建上下文 + 告诉模型“暂停期间发生了什么”。"""
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run_id in self._active:
            raise RuntimeError("该 Run 正在执行中")
        s = self._session(run.session_id)
        if (busy := self.active_run_of(s.id)) is not None:
            raise RuntimeError(f"会话中已有运行中的任务 {busy}")
        note = self._resume_note(run, s)
        self._note_model_switch(run_id, s)
        self._offline_transition(run_id, RunStatus.QUEUED,
                                 "恢复执行" if run.status != RunStatus.FAILED else f"重试（第 {run.attempt + 1} 次尝试）",
                                 expect={RunStatus.PAUSED, RunStatus.INTERRUPTED, RunStatus.FAILED})
        self._launch(run, s, prompt=None, resume=True, resume_note=note)
        return self.store.get_run(run_id)  # type: ignore[return-value]

    def _resume_note(self, run: RunRecord, s: SessionRecord) -> str:
        cp = run.checkpoint or {}
        lines = []
        if run.status == RunStatus.FAILED:
            lines.append(f"上一次尝试以失败结束（{run.error or run.status_reason or '原因未知'}）。现在重试：先检查失败原因，"
                         "不要重复已经成功的步骤。")
        elif run.status == RunStatus.PAUSED and cp.get("paused_at"):
            lines.append(f"本次运行曾暂停 {now() - cp['paused_at']:.0f} 秒，现已恢复。")
        else:
            lines.append("运行进程曾意外中断（崩溃或重启），现已从最近的检查点恢复。未完成的工具调用会先按幂等键对账，"
                         "已生效的副作用不会重复执行。")
        if cp.get("manifest") is not None:
            diff = Workspace.diff_manifest(cp["manifest"], self.workspace(s).manifest())
            if not diff.empty:
                lines.append("在此期间工作区发生了变化（" + diff.describe() + "）。继续之前，请重新读取与任务相关的文件。")
        return "\n".join(lines)

    async def retry(self, run_id: str) -> RunRecord:
        """重试失败的 Run：failed → queued（attempt + 1），从持久化的历史继续，而不是从头再来。"""
        run = self.store.get_run(run_id)
        if run is None or run.status != RunStatus.FAILED:
            raise RuntimeError("只有 failed 状态的 Run 可以重试")
        return await self.resume(run_id)

    def steer(self, run_id: str, text: str) -> bool:
        a = self._active.get(run_id)
        if a is None:
            return False
        a.rc.inbox.steer(text)
        a.hub.publish(Event(E.NOTICE, {"level": "info", "code": "steer_queued", "text": f"插话已排队，将在下一个安全点注入：{clip(text, 80)}"}))
        return True

    def follow_up(self, run_id: str, text: str) -> bool:
        a = self._active.get(run_id)
        if a is None:
            return False
        a.rc.inbox.follow_up(text)
        a.hub.publish(Event(E.NOTICE, {"level": "info", "code": "follow_up_queued", "text": f"已排队，将在当前任务结束后处理：{clip(text, 80)}"}))
        return True

    def resolve_approval(self, approval_id: str, approved: bool, *, by: str = "user", reason: str = "",
                         edited_args: dict | None = None, remember: bool = False) -> bool:
        return self.approvals.resolve(approval_id, ApprovalDecision(approved, by, reason, edited_args, remember))

    async def wait(self, run_id: str) -> RunRecord:
        a = self._active.get(run_id)
        if a is not None and a.task is not None:
            await asyncio.wait({a.task})  # 不会因为任务被取消（例如 fencing）而抛出
        return self.store.get_run(run_id)  # type: ignore[return-value]

    # ================================================================== 崩溃恢复
    async def recover(self, auto_resume: bool = True) -> list[str]:
        """接管心跳过期的 Run（其 Worker 已崩溃）。CAS 迁移本身就是 fencing：两个 Worker 同时接管只有一个成功。"""
        stale = self.store.stale_runs(tuple(ACTIVE), self.config.stale_after_seconds)
        recovered = []
        for run in stale:
            if run.id in self._active:
                continue
            try:
                if run.parent_run_id:
                    # 子 Run 不单独恢复：父 Run 恢复时 task 调用会重新派生新的子代理。这里只把残留的子 Run 收尾
                    if run.status != RunStatus.CANCELLING:
                        self._offline_transition(run.id, RunStatus.INTERRUPTED, "父 Run 所在进程中断",
                                                 expect_version=run.version)
                    self._offline_transition(run.id, RunStatus.CANCELLED, "子代理由父 Run 重新派生")
                    self._pair_dangling_offline(run, "已取消：父 Run 中断，子代理将重新派生")
                    continue
                if run.status == RunStatus.CANCELLING:
                    self._offline_transition(run.id, RunStatus.CANCELLED, "恢复时完成取消", expect_version=run.version)
                    self._pair_dangling_offline(run, "已取消（进程在取消过程中中断）")
                    continue
                # 钉住扫描时看到的版本：扫描之后若已被别的 Worker 接管（版本变了），这里的 CAS 必然失败
                self._offline_transition(run.id, RunStatus.INTERRUPTED, f"Worker {run.worker_id} 心跳超时",
                                         expect_version=run.version)
                for ap in self.store.list_approvals("pending", run.id):
                    self.store.resolve_approval(ap.id, "expired", decided_by="system:recover", reason="进程中断")
            except Exception:
                continue  # 被别的 Worker 抢先接管（CAS 失败）
            recovered.append(run.id)
            if auto_resume:
                await self.resume(run.id)
        # queued 孤儿：创建后、开始执行前进程就退出了；关闭服务时被暂停的 Run：重启后自动继续
        for run in self.store.runs_with_status(RunStatus.QUEUED, RunStatus.PAUSED):
            if run.id in self._active or run.parent_run_id or run.id in recovered:
                continue
            idle = now() - (run.updated_at or 0)
            try:
                if run.status == RunStatus.QUEUED and idle > self.config.stale_after_seconds:
                    s = self._session(run.session_id)
                    started = any(e.run_id == run.id for e in self.store.path(s.id))
                    recovered.append(run.id)
                    if auto_resume:
                        self._launch(run, s, prompt=None if started else run.prompt, resume=started,
                                     resume_note=self._resume_note(run, s) if started else None)
                elif run.status == RunStatus.PAUSED and (run.checkpoint or {}).get("paused_by") == "shutdown":
                    recovered.append(run.id)
                    if auto_resume:
                        await self.resume(run.id)
            except Exception:
                continue
        return recovered

    async def recovery_loop(self, interval: float | None = None) -> None:
        """周期性接管：不只在启动时恢复一次——崩溃的 Worker 心跳要等一会儿才会过期。"""
        while True:
            await asyncio.sleep(interval or max(5.0, self.config.stale_after_seconds))
            with contextlib.suppress(Exception):
                await self.recover(auto_resume=True)

    def _pair_dangling_offline(self, run: RunRecord, text: str) -> None:
        from ..types import tool_result_message

        log = SessionLog(self.store, run.session_id)
        for c in unpaired_tool_calls(log.context().messages):
            step = self.store.get_step(run.id, c.id)
            if step is not None and step.status == "started":  # 已发出、结果未知：不能记成“已取消”
                body, status = f"结果未知：该调用已开始执行，但进程在其完成前中断（幂等键 {step.idem_key}），需要对账确认。", "unknown"
            else:
                body, status = text, "cancelled"
            with self.store.tx():
                log.append_message(tool_result_message(c, body, is_error=True, details={"status": status}), run.id)
                self.store.step_finished(run.id, c.id, status, {"content": body})

    # ================================================================== 订阅（断线续传）
    async def subscribe(self, run_id: str, after_seq: int = 0) -> AsyncIterator[Event]:
        """先从库里补齐 seq > after_seq 的事件，再接上实时流；按 seq 去重，保证不重不漏。"""
        a = self._active.get(run_id)
        q = a.hub.subscribe() if a else None
        try:
            if a:
                a.hub.flush()
            last = after_seq
            for ev in self.store.list_events(run_id, after_seq):
                yield ev
                last = ev.seq or last
            if q is None:
                return
            while True:
                ev = await q.get()
                if ev is _LAGGED:
                    # 与正常路径同序：先重新订阅（之后的事件进新队列），再从库里补齐，最后按 seq 去重
                    a2 = self._active.get(run_id)
                    q = a2.hub.subscribe() if a2 is not None else None
                    if a2 is not None:
                        a2.hub.flush()
                    for ev2 in self.store.list_events(run_id, last):
                        yield ev2
                        last = ev2.seq or last
                    if q is None:
                        return
                    continue
                if ev is _CLOSED:
                    for ev2 in self.store.list_events(run_id, last):  # 关闭前可能还有未投递的尾巴
                        yield ev2
                        last = ev2.seq or last
                    return
                if (ev.seq or 0) <= last:
                    continue
                yield ev
                last = ev.seq or last
        finally:
            if a and q is not None:
                a.hub.unsubscribe(q)

    def flush_if_active(self, run_id: str) -> None:
        a = self._active.get(run_id)
        if a is not None:
            a.hub.flush()

    # ================================================================== 便捷 API
    async def run(self, prompt: str, *, session_id: str | None = None, title: str | None = None,
                  on_event: Callable[[Event], Any] | None = None) -> RunResult:
        """一次性执行：创建（或复用）会话 → 启动 Run → 收集事件直到结束。"""
        if session_id is None:
            session_id = self.create_session(title or clip(prompt, 30)).id
        run = await self.start_run(session_id, prompt)
        events: list[Event] = []
        async for ev in self.subscribe(run.id):
            events.append(ev)
            if on_event is not None:
                r = on_event(ev)
                if asyncio.iscoroutine(r):
                    await r
        rec = await self.wait(run.id)
        res = rec.result or {}
        return RunResult(rec.id, session_id, rec.status, res.get("reason", ""), res.get("text", ""), rec.usage, events)

    async def compact(self, session_id: str) -> dict[str, Any] | None:
        """手动压缩（不在 Run 中）。"""
        if self.active_run_of(session_id):
            raise RuntimeError("会话正在运行，请在运行结束后再手动压缩")
        log = self.log(session_id)
        view = log.context()
        ctxm = ContextManager(self.config.context, self.model_info)
        s = self._session(session_id)
        stats = ctxm.estimate(view.messages, s.meta.get("system_prompt", ""), self.tools.specs())
        res = await Compactor(self.provider, self.model, self.config.context).compact(
            log, view, CancelToken(), tokens_before=stats.tokens, force=True)
        if res:
            self.store.update_session(session_id, meta={**s.meta, "clear_watermark": 0})
        return res.__dict__ if res else None

    def branch(self, session_id: str, entry_id: str | None) -> None:
        """会话内分支：把 head 移到 entry_id，之后的新输入形成新分支（旧分支保留）。"""
        if self.active_run_of(session_id):
            raise RuntimeError("会话正在运行，无法切换分支")
        self.log(session_id).branch(entry_id)
        s = self._session(session_id)
        # 清理水位线是“上下文视图中的消息下标”，换了分支视图就变了，必须归零
        self.store.update_session(session_id, meta={**s.meta, "clear_watermark": 0})

    # ================================================================== 子代理
    async def run_subagent(self, parent: RunContext, *, description: str, prompt: str, tools: list[str] | None,
                           max_turns: int, emit: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        ps = self._session(parent.session_id)
        sp = SUBAGENT_PROMPT.replace("{workspace}", str(parent.workspace.root))
        child_s = self.create_session(f"子代理：{description}", workspace=str(parent.workspace.root),
                                      mode=ps.meta.get("mode"), parent_session_id=parent.session_id, system_prompt=sp)
        child_tools = parent.tools.subset(tools, exclude=["task"] if parent.depth + 1 >= self.config.subagent_max_depth else [])
        budget = parent.budget.child(BudgetConfig(max_turns=max_turns, max_tool_calls=max_turns * 4))
        cancel = parent.cancel.child()  # 父取消 → 子取消
        contract, child_runs, attempt_prompt = None, [], prompt
        for attempt in range(2):  # 违约时给一次修正机会
            run = self.store.create_run(child_s.id, attempt_prompt, parent_run_id=parent.run_id,
                                        config={"role": "subagent", "description": description})
            self._offline_transition(run.id, RunStatus.QUEUED, "子代理已创建")
            rc = self._build_rc(run, child_s, cancel=cancel, inbox=Inbox(), role="subagent", depth=parent.depth + 1,
                                tools=child_tools, budget=budget, blob_scope=parent.scope)
            rc.usage_base = budget.usage  # 多次尝试共用一份子预算：每个子 Run 只记自己这次尝试的用量
            rc.config = _no_verify(self.config)
            # 事件序号接在离线写入的 status 事件之后，否则第一批事件会和它撞号
            a = ActiveRun(run.id, child_s.id, rc, AgentLoop(rc), EventHub(self.store, run.id, self.store.max_event_seq(run.id)))
            rc.set_status = lambda to, reason, a=a: self._set_status(a, to, reason)
            self._active[run.id] = a
            self._transition(a, RunStatus.RUNNING, "子代理开始", expect={RunStatus.QUEUED},
                             worker_id=self.worker_id, heartbeat_at=now())
            hb = asyncio.create_task(self._heartbeat(a))
            try:
                async for ev in a.loop.run(attempt_prompt):
                    a.hub.publish(ev)
                    if ev.type not in EPHEMERAL_EVENTS:
                        emit({"subagent": {"run_id": run.id, "session_id": child_s.id, "description": description,
                                           "event": ev.to_dict()}})
                await self._finish(a)
            except asyncio.CancelledError:
                # 父 Run 被抢占式终止：子 Run 也必须落到明确的终态，不能永远停在 running
                with contextlib.suppress(Exception):
                    self._pair_dangling_offline(self.store.get_run(run.id), "已取消：父 Run 被终止")  # type: ignore[arg-type]
                    if self.store.get_run(run.id).status in (RunStatus.RUNNING, RunStatus.WAITING_HUMAN):  # type: ignore[union-attr]
                        self._transition(a, RunStatus.CANCELLING, "父 Run 被终止")
                    self._transition(a, RunStatus.CANCELLED, "父 Run 被终止")
                raise
            finally:
                hb.cancel()
                a.hub.close()
                self._active.pop(run.id, None)
            child_runs.append(run.id)
            if a.rc.tainted and not parent.tainted:
                # 子代理读过不可信内容：它的结论同样不可信（注入可以“借”回传契约进入父上下文）
                parent.tainted = True
                ps = self.store.get_session(parent.session_id)
                if ps is not None:
                    self.store.update_session(ps.id, meta={**ps.meta, "tainted": True})
            out = a.loop.outcome
            assert out is not None
            if out.status == "cancelled":
                # 父 Run 被取消：向上传播。只有这个子 Run 被单独取消：作为失败的回传交给父代理，由它决定下一步
                parent.cancel.raise_if_cancelled()
                contract = {"status": "failed", "summary": f"子代理被取消：{out.reason}", "findings": [],
                            "artifacts": [], "open_questions": []}
                break
            text = out.final_text or (a.loop._last.text if a.loop._last else "")
            contract, errors = check_contract(text, parent.workspace)
            if not errors and contract is not None:
                break
            if out.status not in ("completed",):
                contract = {"status": "failed", "summary": f"子代理以 {out.status} 结束：{out.reason}", "findings": [],
                            "artifacts": [], "open_questions": []}
                break
            attempt_prompt = ("你的回传不符合契约：\n" + "\n".join(f"- {e}" for e in errors)
                              + "\n请只输出一个符合契约的 JSON 代码块，不要附加其它文字。")
            contract = {"status": "failed", "summary": "子代理回传两次均不符合契约",
                        "findings": [{"claim": clip(text, 500), "evidence": [], "confidence": 0.2}],
                        "artifacts": [], "open_questions": errors}
        return {"contract": contract, "child_session_id": child_s.id, "child_run_ids": child_runs,
                "usage": budget.usage.to_dict()}


def _no_verify(cfg: HarnessConfig) -> HarnessConfig:
    """子代理不跑父任务的验收命令（验收针对的是整体任务）。"""
    from dataclasses import replace

    return replace(cfg, verify_command=None)


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
