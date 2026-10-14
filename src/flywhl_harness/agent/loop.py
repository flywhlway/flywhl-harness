"""Agent Loop：harness 的心脏。

一轮（turn）的阶段
------------------
::

    ┌─► 安全点（取消/暂停检查） ─► 注入插话（steer） ─► 预算检查（软阈值提醒 / 硬阈值收尾）
    │      ─► 上下文整形（水位估算 → 必要时压缩） ─► 注入 system-reminder（计划复述、循环警告…）
    │      ─► 模型调用（流式；重试/降级在接入层完成） ─► assistant 消息落盘（检查点锚点）
    │      ─► 有工具调用？ ──否──► 排队消息（follow-up）？ ─► Stop 验收（hooks + 验证器）─► 结束
    │              │是
    │              ▼
    │         工具执行（权限 → hooks → 审批 → intent → 执行 → 结果按原序落盘）─► 循环守卫
    └──────────────┘

为什么用 async generator？
--------------------------
- **拉取式控制流**：消费方（CLI / Web / SDK / 父代理）按需拉取事件，天然背压；
- **流式透传**：模型的文本增量直接 ``yield`` 给 UI，不需要回调地狱；
- **可组合**：子代理的 Loop 也是一个 generator，父代理逐层透传它的事件；
- **可测试**：注入 ScriptedProvider，断言产出的事件序列即可。

不变量
------
1. 每条带工具调用的 assistant 消息，之后都有与之一一配对的 tool 结果（含合成的“已取消/未执行”结果）；
2. 每个阶段边界都已落盘：进程在任何时刻被 kill，都能从存储重建并继续（见 runtime/manager.py 的恢复）；
3. 结束状态如实区分：completed / incomplete / budget_exhausted / loop_detected / cancelled / failed ……
"""

from __future__ import annotations

import asyncio
import traceback
from dataclasses import dataclass
from typing import Any, AsyncIterator

from .._utils import clip, new_id, now
from ..cancel import RunCancelled, RunPaused
from ..events import E, Event
from ..llm.base import ErrorKind, ModelRequest, ProviderError
from ..llm.replay import context_fingerprint
from ..policy.hooks import HookEvent, HookInput
from ..policy.permissions import Mode
from ..prompts import WRAP_UP_PROMPT, reminder
from ..tools.extra import latest_todos, render_todos
from ..types import Message, MsgKind, StopReason, TextBlock, tool_result_message, unpaired_tool_calls, user_message
from .executor import ToolExecutor
from .run_context import RunContext


@dataclass
class Outcome:
    status: str
    reason: str = ""
    by: str | None = None
    error: str | None = None
    final_text: str = ""


class _ContextOverflow(Exception):
    pass


class AgentLoop:
    def __init__(self, rc: RunContext):
        self.rc = rc
        self.executor = ToolExecutor(rc)
        self.outcome: Outcome | None = None
        self.turn = 0
        self._last: Message | None = None
        self._overflow_retries = 0
        self._stop_note: str | None = None   # Stop hook 不阻止结束时给出的补充信息
        self._hook_warnings: list[str] = []

    # ================================================================== 外层：保证一定以明确状态结束
    async def run(self, prompt: str | None = None) -> AsyncIterator[Event]:
        rc = self.rc
        yield self._ev(E.RUN_START, prompt=prompt, resume=rc.resume, model=rc.model, role=rc.role, depth=rc.depth)
        mi = rc.model_info
        if rc.role == "main" and rc.config.budget.max_cost_usd and not (mi.input_price or mi.output_price) \
                and not rc.provider.reports_cost:
            # 未登记价格的模型费用记为 0：费用预算形同虚设，必须明确告诉用户，而不是悄悄失效
            yield self._ev(E.NOTICE, level="warn", code="unpriced_model",
                           text=f"模型 {rc.model} 未登记价格，费用预算 max_cost_usd 不会生效；请改用 max_total_tokens 限额")
        try:
            async for ev in self._body(prompt):
                yield ev
        except RunCancelled as c:
            for ev in self._pair_dangling(f"已取消：{c.reason}"):
                yield ev
            self.outcome = Outcome("cancelled", c.reason, by=c.by)
        except RunPaused:
            self.outcome = Outcome("paused", "已在安全点暂停")
        except ProviderError as e:
            self.outcome = Outcome("failed", f"模型调用失败：{e}", error=str(e))
        except Exception as e:  # 防御性兜底：任何异常都以 failed 结束，并保留现场
            self.outcome = Outcome("failed", f"{type(e).__name__}: {e}", error=traceback.format_exc())
        if self.outcome is None:
            self.outcome = Outcome("failed", "Loop 未给出结束状态")
        if self._last is not None and not self.outcome.final_text:
            self.outcome.final_text = self._last.text
        yield self._ev(E.USAGE, **rc.budget.snapshot())
        yield self._ev(E.RUN_END, status=self.outcome.status, reason=self.outcome.reason, by=self.outcome.by,
                       final_text=clip(self.outcome.final_text, 4000), usage=rc.run_usage.to_dict(), turns=self.turn)

    # ================================================================== 主体
    async def _body(self, prompt: str | None) -> AsyncIterator[Event]:
        rc = self.rc
        if prompt is not None:
            async for ev in self._accept_prompt(prompt):
                yield ev
            if self.outcome is not None:
                return
        # 修复/恢复：上一次中断遗留的、尚未得到结果的工具调用（崩溃恢复的关键一步）。
        # 必须先于恢复说明处理：tool 结果要紧跟在它的 assistant 调用之后，中间插入任何 user 消息
        # 都会让 OpenAI 兼容协议拒绝请求，也会让 prepare_history 把真实结果当作孤立结果丢弃。
        pending = unpaired_tool_calls(rc.log.context().messages)
        if pending:
            yield self._ev(E.NOTICE, level="info", code="recover",
                           text=f"发现 {len(pending)} 个未完成的工具调用，开始对账/补执行")
            async for ev in self.executor.run_batch(pending, recovering=True):
                yield ev
        if rc.resume and rc.resume_note:
            yield self._append(user_message(reminder(rc.resume_note), kind=MsgKind.RESUME_NOTE))

        while True:
            self._safe_point()
            for text in rc.inbox.drain_steer():
                yield self._append(user_message(text, kind=MsgKind.STEER))

            verdict = rc.budget.check()
            if verdict.state == "hard":
                async for ev in self._wrap_up("；".join(verdict.reasons)):
                    yield ev
                self.outcome = Outcome(verdict.status or "budget_exhausted", "达到限额：" + "；".join(verdict.reasons))
                return
            if verdict.state == "soft":
                rc.remind("预算即将用尽（" + "；".join(verdict.reasons) + "）。请优先完成最关键的部分，并为未完成的工作留下清晰的进度说明。")
            if rc.stop_requested:
                self.outcome = Outcome("cancelled", rc.stop_requested, by="hook")
                return

            async for ev in self._maybe_compact():
                yield ev
            async for ev in self._inject_reminders():
                yield ev

            rc.budget.next_turn()
            self.turn += 1
            yield self._ev(E.TURN_START, turn=self.turn)
            try:
                async for ev in self._call_model():
                    yield ev
            except _ContextOverflow:
                # 上下文超长：不能原样重试——先强制压缩，再重试这一轮（每轮最多一次）
                yield self._ev(E.TURN_END, turn=self.turn, aborted="context_overflow")
                self._overflow_retries += 1
                if self._overflow_retries > 1:
                    raise ProviderError(ErrorKind.CONTEXT_OVERFLOW, "压缩后上下文仍然超长")
                async for ev in self._maybe_compact(force=True):
                    yield ev
                continue
            self._overflow_retries = 0
            msg = self._last
            assert msg is not None
            rc.budget.charge_model(msg.usage)
            yield self._ev(E.USAGE, **rc.budget.snapshot())

            if msg.stop_reason == StopReason.REFUSAL:
                yield self._ev(E.TURN_END, turn=self.turn)
                self.outcome = Outcome("failed", "模型基于安全策略拒绝了该请求（refusal）")
                return
            calls = msg.tool_calls
            if calls and msg.stop_reason == StopReason.MAX_TOKENS:
                # 输出被截断时，工具参数可能不完整：整批判失败、不执行，让模型分步重来
                for c in calls:
                    yield self._persist_tool_error(c, "输出被 max_tokens 截断，该调用的参数可能不完整，未执行。请减少单次输出（例如分多次写入文件）后重新调用。", "truncated")
                yield self._ev(E.TURN_END, turn=self.turn)
                continue
            if not calls and msg.stop_reason == StopReason.MAX_TOKENS:
                # 纯文本回复被长度上限截断：不能当作“完成”，让模型从中断处续写（有上限，防止无限续写）
                n = rc.state.get("continuations", 0)
                if n < 3:
                    rc.state["continuations"] = n + 1
                    yield self._append(user_message(reminder(
                        "你的上一条回复达到输出长度上限被截断。请直接从中断处继续，不要重复已经输出的内容。"),
                        kind=MsgKind.REMINDER))
                    yield self._ev(E.TURN_END, turn=self.turn)
                    continue
                yield self._ev(E.NOTICE, level="warn", code="truncated", text="回复多次被长度上限截断，可能不完整")
            if not calls:
                if rc.inbox.pending()["steer"] or rc.inbox.pending()["follow_up"]:
                    for ev in self._drain_inbox():
                        yield ev
                    yield self._ev(E.TURN_END, turn=self.turn)
                    continue
                gaps = await self._stop_gaps(msg)
                for ev in self._hook_notices():
                    yield ev
                if gaps:
                    if rc.stop_blocks < rc.config.max_stop_blocks:
                        rc.stop_blocks += 1
                        yield self._ev(E.NOTICE, level="warn", code="stop_blocked",
                                       text=f"验收未通过，第 {rc.stop_blocks} 次阻止结束", gaps=gaps)
                        yield self._append(user_message(reminder(
                            "你准备结束，但验收尚未通过：\n" + "\n".join(f"- {g}" for g in gaps)
                            + "\n请继续完成；如果确实无法完成，请如实说明原因。"), kind=MsgKind.STOP_FEEDBACK))
                        yield self._ev(E.TURN_END, turn=self.turn)
                        continue
                    self.outcome = Outcome("incomplete", "多次验收未通过：" + "；".join(gaps))
                    yield self._ev(E.TURN_END, turn=self.turn)
                    return
                if rc.inbox.pending()["steer"] or rc.inbox.pending()["follow_up"]:
                    # 验收（可能要跑测试）期间又有新输入到达：继续处理，而不是丢掉
                    for ev in self._drain_inbox():
                        yield ev
                    yield self._ev(E.TURN_END, turn=self.turn)
                    continue
                if self._stop_note:
                    yield self._append(user_message(reminder(self._stop_note), kind=MsgKind.REMINDER))
                    self._stop_note = None
                self.outcome = Outcome("completed", "自然完成", final_text=msg.text)
                yield self._ev(E.TURN_END, turn=self.turn)
                return

            async for ev in self.executor.run_batch(calls):
                yield ev
            rc.budget.charge_tool(len(calls))
            stop_msg = None
            for c in calls:
                res = self.executor.last_results.get(c.id)
                if res is None:
                    continue
                v = rc.loop_guard.observe(c.name, c.arguments, res.text, res.is_error)
                if v.action == "stop":
                    stop_msg = v.message
                elif v.action == "warn":
                    rc.remind(v.message)
                    yield self._ev(E.NOTICE, level="warn", code="loop_guard", text=v.message)
            yield self._ev(E.TURN_END, turn=self.turn)
            if stop_msg:
                self.outcome = Outcome("loop_detected", stop_msg)
                return

    # ================================================================== 阶段实现
    async def _accept_prompt(self, prompt: str) -> AsyncIterator[Event]:
        rc = self.rc
        extra = None
        if rc.hooks.has(HookEvent.USER_PROMPT_SUBMIT):
            out = await self._hook(HookEvent.USER_PROMPT_SUBMIT, HookInput(
                event=HookEvent.USER_PROMPT_SUBMIT, run_id=rc.run_id, session_id=rc.session_id,
                cwd=str(rc.workspace.root), prompt=prompt, context=rc))
            for ev in self._hook_notices():
                yield ev
            if out.decision in ("block", "deny"):
                self.outcome = Outcome("failed", f"输入被 hook 拦截：{out.reason}", by="hook")
                return
            extra = out.additional_context
        yield self._append(user_message(prompt, kind=MsgKind.PROMPT))
        if extra:
            rc.remind(extra)

    def _safe_point(self) -> None:
        self.rc.cancel.raise_if_cancelled()
        if self.rc.pause_requested:
            raise RunPaused()

    async def _maybe_compact(self, force: bool = False) -> AsyncIterator[Event]:
        rc = self.rc
        view = rc.log.context()
        stats = rc.context.estimate(view.messages, rc.system_prompt, rc.tools.specs())
        if not force and not rc.context.should_compact(stats):
            return
        skipped_at = rc.state.get("compact_skipped_at")
        if not force and skipped_at is not None and stats.tokens < skipped_at * 1.15:
            return  # 上次已确认“没有可压缩的历史”，水位没有明显增长前不再空转
        extra = None
        if rc.hooks.has(HookEvent.PRE_COMPACT):
            out = await self._hook(HookEvent.PRE_COMPACT, HookInput(
                event=HookEvent.PRE_COMPACT, run_id=rc.run_id, session_id=rc.session_id, context=rc))
            extra = out.additional_context
            for ev in self._hook_notices():
                yield ev
        yield self._ev(E.COMPACTION_START, tokens=stats.tokens, window=stats.window, threshold=stats.compact_at, forced=force)
        res = await rc.compactor.compact(rc.log, view, rc.cancel, tokens_before=stats.tokens,
                                         extra_instructions=extra, run_id=rc.run_id, force=force, window=stats.window)
        if res is None:
            rc.state["compact_skipped_at"] = stats.tokens
            yield self._ev(E.COMPACTION_END, skipped=True, reason="没有可压缩的历史（最近的消息本身就很大，压缩无法再降低水位）")
            return
        rc.state.pop("compact_skipped_at", None)
        rc.budget.charge_model(res.usage)  # 摘要调用本身也消耗 token，计入预算
        rc.clear_watermark = 0  # 消息下标在压缩后整体变化，清理水位线随之归零
        self._save_session_meta(clear_watermark=0)
        yield self._ev(E.COMPACTION_END, entry_id=res.entry_id, tokens_before=res.tokens_before,
                       tokens_after=res.tokens_after, summarized=res.summarized, fallback=res.used_fallback,
                       summary=clip(res.summary, 3000), **res.details)

    async def _inject_reminders(self) -> AsyncIterator[Event]:
        rc = self.rc
        # 计划从当前分支的完整历史重建：即使最后一次 todo_write 已被压缩进摘要，计划也不会“消失”
        todo = rc.context.todo_reminder(rc.log.messages())
        if todo:
            rc.remind(todo)
        if rc.pending_reminders:
            text = "\n\n".join(rc.pending_reminders)
            rc.pending_reminders.clear()
            # 提醒作为一条用户消息 **追加并落盘**：历史保持只追加，回放时模型看到的与当时完全一致
            yield self._append(user_message(reminder(text), kind=MsgKind.REMINDER))

    async def _call_model(self, *, purpose: str = "turn", tool_choice: str = "auto") -> AsyncIterator[Event]:
        rc = self.rc
        view = rc.log.context()
        specs = rc.tools.specs()
        stats = rc.context.estimate(view.messages, rc.system_prompt, specs)
        messages, wm = rc.context.apply_clearing(view.messages, stats, rc.clear_watermark)
        if wm != rc.clear_watermark:
            rc.clear_watermark = wm
            self._save_session_meta(clear_watermark=wm)
            yield self._ev(E.NOTICE, level="info", code="clear_tool_results", text=f"已清理第 {wm} 条之前的大工具结果")
        req = ModelRequest(
            model=rc.model, system=rc.system_prompt, messages=messages, tools=specs,
            max_tokens=rc.config.max_tokens, temperature=rc.config.temperature, tool_choice=tool_choice,
            purpose=purpose, metadata={"role": rc.role, "run_id": rc.run_id, "session_id": rc.session_id},
        )
        mid = new_id("m_")
        # 上下文指纹：回放时比对“模型看到的东西”是否与录制时一致，用来定位分叉点
        yield self._ev(E.MESSAGE_START, message_id=mid, role="assistant", context=stats.to_dict(),
                       fingerprint=context_fingerprint(req), purpose=purpose)
        text_acc: list[str] = []
        final: Message | None = None
        try:
            async for sev in rc.cancel.iterate(rc.provider.stream(req, rc.cancel)):
                t = sev.type
                if t == "text_delta":
                    text_acc.append(sev.delta)
                    yield self._ev(E.MESSAGE_UPDATE, message_id=mid, kind="text", delta=sev.delta, index=sev.index)
                elif t == "thinking_delta":
                    yield self._ev(E.MESSAGE_UPDATE, message_id=mid, kind="thinking", delta=sev.delta, index=sev.index)
                elif t == "toolcall_start":
                    yield self._ev(E.MESSAGE_UPDATE, message_id=mid, kind="toolcall_start", index=sev.index, **sev.info)
                elif t == "toolcall_delta":
                    yield self._ev(E.MESSAGE_UPDATE, message_id=mid, kind="toolcall", delta=sev.delta, index=sev.index)
                elif t == "retry":
                    if sev.info.get("discard_partial"):
                        text_acc.clear()
                    yield self._ev(E.MODEL_RETRY, message_id=mid, **sev.info)
                elif t == "notice":
                    yield self._ev(E.NOTICE, level="info", **sev.info)
                elif t == "done":
                    final = sev.message
        except RunCancelled:
            # 流式中途被取消：保留已生成的文本（标记 aborted），丢弃半截工具调用
            if text_acc:
                partial = Message(role="assistant", content=[TextBlock("".join(text_acc))], id=mid,
                                  stop_reason=StopReason.ABORTED, model=rc.model, provider=rc.provider.name)
                yield self._append(partial)
            raise
        except ProviderError as e:
            if e.kind == ErrorKind.CONTEXT_OVERFLOW:
                raise _ContextOverflow() from e
            err = Message(role="assistant", content=[], id=mid, stop_reason=StopReason.ERROR, error=str(e),
                          model=rc.model, provider=rc.provider.name)
            yield self._append(err)
            raise
        if final is None:
            raise ProviderError(ErrorKind.UNKNOWN, "provider 没有返回最终消息")
        final.id, final.ts = mid, now()
        self._last = final
        yield self._append(final)

    async def _wrap_up(self, reason: str) -> AsyncIterator[Event]:
        """预算耗尽：不直接截断，而是让模型做一次 **无工具** 的总结性收尾，留下可交接的产物。"""
        rc = self.rc
        if not rc.config.budget.wrap_up_on_exhaust:
            return
        yield self._ev(E.NOTICE, level="warn", code="budget_exhausted", text=f"达到限额（{reason}），进入收尾")
        yield self._append(user_message(WRAP_UP_PROMPT.format(reason=reason), kind=MsgKind.WRAP_UP))
        self.turn += 1
        yield self._ev(E.TURN_START, turn=self.turn, wrap_up=True)
        try:
            async for ev in self._call_model(purpose="wrap_up", tool_choice="none"):
                yield ev
        except (ProviderError, _ContextOverflow):
            yield self._ev(E.TURN_END, turn=self.turn, aborted="wrap_up_failed")
            return
        if self._last is not None:
            rc.budget.charge_model(self._last.usage)
            for c in self._last.tool_calls:  # 模型无视了 tool_choice=none：不执行，只补结果保证配对
                yield self._persist_tool_error(c, "未执行：已达到限额，收尾阶段不允许调用工具。", "skipped")
        yield self._ev(E.TURN_END, turn=self.turn)

    async def _stop_gaps(self, msg: Message) -> list[str]:
        """Stop 验收：完成是产物的属性，不是对话的属性。返回未满足的验收项。"""
        rc = self.rc
        gaps: list[str] = []
        # 规划模式的产物就是计划本身：计划项尚未执行、验收命令跑不过都是预期状态。此时再拿它们阻止结束，
        # 只会逼模型反复尝试被禁止的写操作（真实模型上实测：3 次阻止、触发循环守卫、以 incomplete 结束）
        planning = rc.permissions.mode == Mode.PLAN
        if rc.config.verify_todos and not planning:
            # 只验收本次 Run 写下的计划：同一会话里上一个任务遗留的计划不应阻止新任务结束
            mine = [Message.from_dict(e.payload) for e in rc.log.path() if e.kind == "message" and e.run_id == rc.run_id]
            todos = latest_todos(mine)
            if todos and any(t.get("status") in ("pending", "in_progress") for t in todos):
                gaps.append("计划中仍有未完成项：\n" + render_todos([t for t in todos if t.get("status") in ("pending", "in_progress")]))
        if rc.config.verify_command and rc.role == "main" and not planning:
            ok, out = await _run_verify(rc.config.verify_command, str(rc.workspace.root))
            if not ok:
                gaps.append(f"验收命令 `{rc.config.verify_command}` 未通过：\n{out}")
        event = HookEvent.STOP if rc.role == "main" else HookEvent.SUBAGENT_STOP
        if rc.hooks.has(event):
            out = await self._hook(event, HookInput(event=event, run_id=rc.run_id, session_id=rc.session_id,
                                                      cwd=str(rc.workspace.root), stop_blocks=rc.stop_blocks,
                                                      last_message=msg.text, context=rc))
            if out.decision == "block":
                gaps.append(out.reason or "Stop hook 阻止结束")
                if out.additional_context:
                    gaps.append(out.additional_context)
            elif out.additional_context:
                self._stop_note = out.additional_context  # 不阻止结束：作为提醒落盘，留给本会话的下一次 Run
            if out.stop_run:
                rc.stop_requested = out.reason
        return gaps

    # ================================================================== 小工具
    async def _hook(self, event: str, inp: HookInput):
        """运行 hook 并收集警告（fail-open 的失败、无法识别的决策）：警告必须让人看得到，不能只留在返回值里。"""
        out = await self.rc.hooks.run(event, inp)
        self._hook_warnings.extend(out.warnings)
        return out

    def _hook_notices(self) -> list[Event]:
        evs = [self._ev(E.NOTICE, level="warn", code="hook_warning", text=w) for w in self._hook_warnings]
        self._hook_warnings.clear()
        return evs

    def _drain_inbox(self) -> list[Event]:
        """模型准备结束时取出排队的输入：插话（steer）在前，追加任务（follow_up）在后。"""
        evs = [self._append(user_message(t, kind=MsgKind.STEER)) for t in self.rc.inbox.drain_steer()]
        evs += [self._append(user_message(t, kind=MsgKind.FOLLOW_UP)) for t in self.rc.inbox.drain_follow_up()]
        return evs

    def _ev(self, type_: str, **data: Any) -> Event:
        return Event(type_, data, run_id=self.rc.run_id)

    def _append(self, msg: Message) -> Event:
        """消息落盘（检查点）后再发 message_end：事件永远不会领先于持久化状态。"""
        self.rc.log.append_message(msg, self.rc.run_id)
        return self._ev(E.MESSAGE_END, message=msg.to_dict())

    def _persist_tool_error(self, call, text: str, status: str) -> Event:
        m = tool_result_message(call, text, is_error=True, details={"status": status})
        self.rc.log.append_message(m, self.rc.run_id)
        return self._ev(E.TOOL_END, call_id=call.id, name=call.name, status=status, is_error=True, content=text,
                        details={"status": status}, message=m.to_dict())

    def _pair_dangling(self, text: str):
        """为未配对的工具调用补合成结果：会话可以继续，下一次请求不会因配对缺失而被 API 拒绝。"""
        for c in unpaired_tool_calls(self.rc.log.context().messages):
            self.rc.store.step_finished(self.rc.run_id, c.id, "cancelled", {"content": text})
            yield self._persist_tool_error(c, text, "cancelled")

    def _save_session_meta(self, **kv: Any) -> None:
        s = self.rc.store.get_session(self.rc.session_id)
        if s is not None:
            self.rc.store.update_session(self.rc.session_id, meta={**s.meta, **kv})


async def _run_verify(command: str, cwd: str, timeout: float = 300.0) -> tuple[bool, str]:
    proc = await asyncio.create_subprocess_shell(command, cwd=cwd, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return False, f"验收命令超时（{timeout:.0f}s）"
    text = out.decode("utf-8", "replace")
    return proc.returncode == 0, "\n".join(text.splitlines()[-30:])
