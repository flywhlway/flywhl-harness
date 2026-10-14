"""工具执行器：一次工具调用从“模型想调用”到“结果写回历史”的完整管线。

::

    预处理 ──► 权限判定 ──► PreToolUse hooks ──► (审批) ──► 预写 intent ──► 执行 ──► PostToolUse hooks ──► 外置大结果 ──► 落盘
    (工具存在?    (deny>ask>     (可改参数→重新校验      (waiting_    (journal      (超时/取消/    (审计/脱敏)         (句柄)          (按原序)
     参数合法?)    allow)          并重新判定)            human)       + 快照)       瞬时重试)

并发策略：把同一轮的调用按顺序切成连续批次——只读且并发安全的调用组成并行批（有界并发），
其它调用严格串行；结果 **按模型给出的原始顺序** 写回历史（保证配对与回放的确定性）。

所有失败都变成 **模型可读的错误结果**（is_error=True），而不是抛异常中断 Loop：
未知工具、参数非法、权限拒绝、审批拒绝、超时、执行异常……模型读到后可以自我修正。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

from .._utils import clip, now, short_hash
from ..cancel import RunCancelled
from ..events import E, Event
from ..policy.hooks import HookEvent, HookInput
from ..policy.permissions import Decision, grant_for, strictest
from ..runtime.workspace import PathEscape
from ..tools.base import Effect, Tool, ToolContext, ToolResult, TransientToolError
from ..tools.schema import format_errors, validate
from ..types import Message, ToolCall, tool_result_message
from .run_context import RunContext


@dataclass
class Prepared:
    call: ToolCall
    tool: Tool | None
    args: dict[str, Any]
    error: str | None = None

    @property
    def parallel(self) -> bool:
        return self.tool is not None and self.error is None and self.tool.concurrency_safe \
            and self.tool.effect_for(self.args) == Effect.READ


class ToolExecutor:
    def __init__(self, rc: RunContext):
        self.rc = rc
        self.last_results: dict[str, Message] = {}

    # ================================================================== 入口
    async def run_batch(self, calls: list[ToolCall], *, recovering: bool = False) -> AsyncIterator[Event]:
        """执行同一条 assistant 消息中的全部工具调用，产出事件；保证每个调用最终都有一条结果写入历史。"""
        rc = self.rc
        queue: asyncio.Queue[Event | None] = asyncio.Queue()
        results: dict[str, Message] = {}
        persisted: set[str] = set()
        self.last_results = results

        def publish(type_: str, data: dict[str, Any]) -> None:
            queue.put_nowait(Event(type_, data, run_id=rc.run_id))

        prepared = [self._prepare(c) for c in calls]
        batches: list[list[Prepared]] = []
        for p in prepared:
            if batches and p.parallel and batches[-1][0].parallel:
                batches[-1].append(p)
            else:
                batches.append([p])

        def persist(p: Prepared) -> None:
            msg = results[p.call.id]
            with rc.store.tx():
                rc.log.append_message(msg, rc.run_id)
                rc.store.step_finished(rc.run_id, p.call.id, (msg.details or {}).get("status", "ok"),
                                       {"is_error": msg.is_error, "content": clip(msg.text, 2000)})
            persisted.add(p.call.id)
            publish(E.TOOL_END, self._end_data(p.call, msg))  # 落盘之后才发布：事件不领先于持久化

        async def driver() -> None:
            abrupt = False
            try:
                for batch in batches:
                    if rc.cancel.cancelled:
                        break
                    if len(batch) > 1:  # 只读并行批：有界并发
                        sem = asyncio.Semaphore(max(1, rc.config.parallel_tool_limit))

                        async def one(p: Prepared) -> None:
                            async with sem:
                                results[p.call.id] = await self._run_one(p, publish, recovering)

                        await asyncio.gather(*(one(p) for p in batch))
                    else:
                        results[batch[0].call.id] = await self._run_one(batch[0], publish, recovering)
                    for p in batch:  # 按原序落盘
                        if p.call.id in results and p.call.id not in persisted:
                            persist(p)
            except asyncio.CancelledError:
                # 抢占式终止（进程关闭 / 被强制取消）：等同于崩溃——在途调用的结果未知，
                # 不能写“未执行”。保持 journal 中的 started 状态，交给恢复流程按幂等键对账。
                abrupt = True
                raise
            finally:
                # 协作式取消或异常：尚未执行的调用补一条合成结果，保证 tool_call / tool_result 成对
                reason = rc.cancel.reason or "运行已终止"
                for p in ([] if abrupt else prepared):
                    if p.call.id not in persisted:
                        if p.call.id not in results:
                            results[p.call.id] = self._result_msg(p.call, ToolResult.error(f"未执行：{reason}"), "skipped", 0, None)
                        try:
                            persist(p)
                        except Exception:  # pragma: no cover - 存储故障时只能放弃
                            pass
                queue.put_nowait(None)

        task = asyncio.create_task(driver())
        try:
            while True:
                ev = await queue.get()
                if ev is None:
                    break
                yield ev
            await task
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    # ================================================================== 预处理
    def _prepare(self, call: ToolCall) -> Prepared:
        tool = self.rc.tools.get(call.name)
        if tool is None:
            return Prepared(call, None, call.arguments,
                            f"未知工具：{call.name}。可用工具：{', '.join(self.rc.tools.names())}。")
        if call.parse_error:
            return Prepared(call, tool, {}, f"工具 {call.name} 的参数不是合法 JSON（{call.parse_error}）。"
                                            f"原始参数片段：{clip(call.raw_arguments or '', 300)}。请重新生成完整、合法的 JSON 参数。")
        args, errors = validate(tool.parameters, call.arguments)
        if errors:
            return Prepared(call, tool, call.arguments, format_errors(call.name, errors))
        return Prepared(call, tool, args)

    # ================================================================== 单个调用
    async def _run_one(self, p: Prepared, publish, recovering: bool) -> Message:
        rc, call, tool = self.rc, p.call, p.tool
        t0 = time.monotonic()
        effect = tool.effect_for(p.args) if tool and not p.error else Effect.READ
        publish(E.TOOL_START, {
            "call_id": call.id, "name": call.name, "args": call.arguments, "effect": str(effect),
            "summary": tool.describe_call(p.args) if tool else call.name, "recovering": recovering,
        })

        def done(result: ToolResult, status: str, snapshot: str | None = None) -> Message:
            # tool_end 由 run_batch 在结果落盘后统一发布
            return self._result_msg(call, result, status, int((time.monotonic() - t0) * 1000), snapshot)

        if p.error or tool is None:
            return done(ToolResult.error(p.error or "工具不可用"), "invalid")
        args = p.args
        ctx = self._ctx(call, publish, recovering)

        # ---- 崩溃恢复：有 intent 无 result 的调用先对账 ----
        if recovering:
            step = rc.store.get_step(rc.run_id, call.id)
            if step is not None and step.status == "started":
                rec = await tool.reconcile(args, ctx)
                if rec is not None:
                    return done(rec, "reconciled")
                if not (effect == Effect.READ or tool.idempotent):
                    return done(ToolResult.error(
                        "上次执行在进程中断时结果未知，且该工具不支持对账。请先核实外部状态（例如查询目标系统），"
                        f"不要盲目重试。幂等键：{ctx.idempotency_key}"), "unknown")
                publish(E.NOTICE, {"level": "info", "code": "recover_reexecute",
                                   "text": f"{call.name} 结果未知，但它是只读/幂等操作，将用同一幂等键重新执行"})

        # ---- 权限 + PreToolUse hooks ----
        perm = rc.permissions.evaluate(tool, args, tainted=rc.tainted, grants=rc.grants)
        decision, reason = perm.decision, perm.reason
        if rc.hooks.has(HookEvent.PRE_TOOL_USE):
            out = await rc.hooks.run(HookEvent.PRE_TOOL_USE, self._hook_input(HookEvent.PRE_TOOL_USE, call.name, args))
            for w in out.warnings:
                publish(E.NOTICE, {"level": "warn", "code": "hook_warning", "text": w})
            if out.updated_input is not None:
                new_args, errs = validate(tool.parameters, out.updated_input)
                if errs:
                    return done(ToolResult.error("PreToolUse hook 改写后的参数未通过校验：\n" + "\n".join(errs)), "invalid")
                args = new_args
                perm = rc.permissions.evaluate(tool, args, tainted=rc.tainted, grants=rc.grants)  # 改写后必须重新判定
                decision, reason = perm.decision, perm.reason
            if out.decision in ("allow", "ask", "deny"):
                # hook 只能收紧、不能放宽：hook 说 allow 也绕不过规则的 ask/deny
                stricter = strictest(decision, out.decision)
                if stricter != decision:
                    decision, reason = stricter, out.reason or f"PreToolUse hook 要求 {out.decision}"
            if out.additional_context:
                rc.remind(out.additional_context)
            if out.stop_run:
                rc.stop_requested = out.reason or "hook 要求终止运行"

        if decision == Decision.DENY:
            return done(ToolResult.error(f"权限拒绝：{reason}。请换一种不需要该权限的方式完成，或向用户说明需要什么权限。"), "denied")
        if decision == Decision.ASK:
            preview = tool.preview(args, ctx)
            dec = await rc.approvals.request(
                run_id=rc.run_id, session_id=rc.session_id, call_id=call.id, tool=tool.name, args=args,
                summary=tool.describe_call(args), risk=perm.risk, reason=reason, cancel=rc.cancel,
                on_created=lambda rec: self._on_approval_created(rec, publish, perm, preview),
            )
            await self._set_status("running", "审批已处理")
            publish(E.APPROVAL_RESOLVED, {"call_id": call.id, "approved": dec.approved, "by": dec.by,
                                          "reason": dec.reason, "remember": dec.remember})
            if not dec.approved:
                why = f"：{dec.reason}" if dec.reason else ""
                return done(ToolResult.error(f"用户拒绝了该操作{why}。请调整方案（例如改用只读方式），或询问用户的意图。"), "rejected")
            if dec.edited_args:
                new_args, errs = validate(tool.parameters, dec.edited_args)
                if errs:
                    return done(ToolResult.error("审批人修改后的参数未通过校验：\n" + "\n".join(errs)), "invalid")
                args = new_args
                recheck = rc.permissions.evaluate(tool, args, tainted=rc.tainted, grants=rc.grants)  # 防 TOCTOU
                if recheck.decision == Decision.DENY:
                    return done(ToolResult.error(f"修改后的参数被权限策略拒绝：{recheck.reason}"), "denied")
                if rc.hooks.has(HookEvent.PRE_TOOL_USE):
                    # 审批人改过的参数同样要过 PreToolUse 守卫：hook 表达的是“不变量”，人工批准不能绕过它
                    again = await rc.hooks.run(HookEvent.PRE_TOOL_USE, self._hook_input(HookEvent.PRE_TOOL_USE, call.name, args))
                    for w in again.warnings:
                        publish(E.NOTICE, {"level": "warn", "code": "hook_warning", "text": w})
                    if again.decision == "deny":
                        return done(ToolResult.error(f"修改后的参数被 PreToolUse hook 拒绝：{again.reason}"), "denied")
                    if again.updated_input is not None:
                        args, errs = validate(tool.parameters, again.updated_input)
                        if errs:
                            return done(ToolResult.error("PreToolUse hook 改写后的参数未通过校验：\n" + "\n".join(errs)), "invalid")
            if dec.remember:
                g = grant_for(tool, args)
                rc.grants.append(g)
                self._persist_grants()
                publish(E.NOTICE, {"level": "info", "code": "grant", "text": f"已添加会话级授权：{g}"})

        # ---- 最后一个安全点：审批/hooks 期间可能已经收到取消；副作用发出之前必须再检查一次 ----
        rc.cancel.raise_if_cancelled()

        # ---- 预写 intent（+ 写操作前快照）----
        effect = tool.effect_for(args)
        snapshot = None
        if effect != Effect.READ and rc.config.snapshots and rc.workspace.shadow is not None:
            snapshot = await rc.workspace.shadow.snapshot(f"before {tool.name} {call.id}")
        rc.store.step_started(rc.run_id, call.id, tool=tool.name, effect=str(effect),
                              idem_key=ctx.idempotency_key, input_hash=short_hash(args), snapshot=snapshot)

        # ---- 执行 ----（耗时只统计执行本身，不含等待审批的时间）
        t_exec = time.monotonic()
        approval_wait_ms = int((t_exec - t0) * 1000)
        result, status = await self._invoke(tool, args, ctx)
        exec_ms = int((time.monotonic() - t_exec) * 1000)

        # ---- PostToolUse hooks ----
        if rc.hooks.has(HookEvent.POST_TOOL_USE):
            hi = self._hook_input(HookEvent.POST_TOOL_USE, call.name, args)
            hi.tool_result = {"content": result.content, "is_error": result.is_error, "details": result.details}
            out = await rc.hooks.run(HookEvent.POST_TOOL_USE, hi)
            for w in out.warnings:
                publish(E.NOTICE, {"level": "warn", "code": "hook_warning", "text": w})
            if out.updated_result is not None:
                result = ToolResult(out.updated_result, result.is_error, result.details, result.tainted)
            if out.decision == "block":
                result = ToolResult(f"{result.content}\n\n[PostToolUse 反馈] {out.reason}", True, result.details, result.tainted)
            if out.additional_context:
                rc.remind(out.additional_context)

        # ---- 外置大结果 + 污点 ----
        result = rc.context.shape_tool_result(result, rc.store.blobs, rc.scope)
        if (result.tainted or tool.untrusted_for(args)) and not rc.tainted:
            rc.tainted = True
            self._persist_session_meta(tainted=True)  # 污点属于会话：暂停恢复、崩溃接管之后依然有效
            publish(E.NOTICE, {"level": "info", "code": "tainted",
                               "text": f"{call.name} 的输出来自不可信来源：之后的副作用操作将需要人工确认"})
        msg = self._result_msg(call, result, status, exec_ms, snapshot)
        if decision == Decision.ASK:
            msg.details["approval_wait_ms"] = approval_wait_ms  # type: ignore[index]
        return msg

    # ================================================================== 执行：超时 / 取消 / 瞬时重试
    async def _invoke(self, tool: Tool, args: dict[str, Any], ctx: ToolContext) -> tuple[ToolResult, str]:
        rc = self.rc
        timeout = float(tool.timeout or rc.config.tool_timeout)
        left = rc.budget.deadline_left()
        ctx.time_left = left
        if left is not None:
            timeout = max(1.0, min(timeout, left))  # 截止时间沿调用链传递
        if tool.manages_timeout:
            # 工具自己会在超时后清理并返回部分结果；执行器的计时器只做兜底，留出 10 秒余量
            own = float(args.get("timeout") or getattr(tool, "default_timeout", timeout))
            timeout = (min(own, left) if left is not None else own) + 10
        attempt = 0
        while True:
            try:
                return await self._race(tool, args, ctx, timeout)
            except TransientToolError as e:
                attempt += 1
                retryable = tool.effect_for(args) == Effect.READ or tool.idempotent
                delay = 0.2 * 2 ** attempt
                left_now = rc.budget.deadline_left()
                if retryable and attempt < 3 and (left_now is None or left_now > delay + 1):
                    await rc.cancel.sleep(delay)
                    continue
                tried = f"共尝试 {attempt} 次" if retryable else "非幂等工具不自动重试"
                return ToolResult.error(f"工具暂时不可用（{tried}）：{e}"), "error"
            except PathEscape as e:
                return ToolResult.error(f"{e}。只能操作工作区内的文件。"), "denied"
            except RunCancelled as e:  # 例如 task：父 Run 被取消时子代理向上传播的取消
                return ToolResult.error(f"已取消：{e}"), "cancelled"
            except Exception as e:
                return ToolResult.error(f"工具 {tool.name} 执行异常：{type(e).__name__}: {e}"), "error"

    async def _race(self, tool: Tool, args: dict[str, Any], ctx: ToolContext, timeout: float) -> tuple[ToolResult, str]:
        """让工具执行与“取消请求”“超时”赛跑，并按工具的可中断性分别处理。"""
        rc = self.rc
        task = asyncio.ensure_future(tool.execute(args, ctx))
        waiter = asyncio.ensure_future(rc.cancel.wait())
        try:
            done, _ = await asyncio.wait({task, waiter}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            task.cancel()
            raise
        finally:
            waiter.cancel()
        if task in done:
            r = task.result()
            if (r.details or {}).get("timed_out") and tool.effect_for(args) == Effect.EXTERNAL and not tool.idempotent:
                # 工具自己管理超时（bash）时也一样：外部命令（git push、curl -X POST）超时 ≠ 没发生
                return ToolResult(f"{r.content}\n[结果未知：命令可能已经对外部系统生效（幂等键 {ctx.idempotency_key}）。"
                                  "请先核实外部状态，不要直接重试。]", True, r.details, r.tainted), "unknown"
            return r, ("error" if r.is_error else "ok")
        if rc.cancel.cancelled:
            if tool.interruptible_for(args):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                return ToolResult.error(f"已取消：{rc.cancel.reason}（工具执行被中断）"), "cancelled"
            # 不可中断（已发出的外部请求）：给一个宽限期等它完成，而不是半途打断
            try:
                r = await asyncio.wait_for(asyncio.shield(task), tool.cancel_grace or rc.config.cancel_grace_seconds)
                r.details = {**(r.details or {}), "completed_after_cancel": True}
                return r, ("error" if r.is_error else "ok")
            except asyncio.TimeoutError:
                task.cancel()
                return ToolResult.error(
                    f"结果未知：取消时该操作仍在进行（幂等键 {ctx.idempotency_key}）。它可能已经生效，需要对账确认。"), "unknown"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        if tool.effect_for(args) == Effect.EXTERNAL and not tool.idempotent:
            # 外部副作用超时 ≠ 没发生：请求可能已经被对方处理，只是响应没回来
            return ToolResult.error(
                f"工具执行超时（{timeout:.0f}s），结果未知：请求可能已经生效（幂等键 {ctx.idempotency_key}）。"
                "请先核实外部状态，不要直接重试。"), "unknown"
        return ToolResult.error(f"工具执行超时（{timeout:.0f}s），已中止。可以缩小范围后重试，或设置更长的超时。"), "timeout"

    # ================================================================== 辅助
    def _ctx(self, call: ToolCall, publish, recovering: bool) -> ToolContext:
        rc = self.rc
        return ToolContext(
            run_id=rc.run_id, session_id=rc.scope, call_id=call.id, workspace=rc.workspace,
            cancel=rc.cancel, blobs=rc.store.blobs, idempotency_key=f"{rc.run_id}:{call.id}",
            emit=lambda u: publish(E.TOOL_UPDATE, {"call_id": call.id, "update": u}),
            state=rc.state, harness=rc.harness, run=rc, recovering=recovering,
        )

    def _hook_input(self, event: str, tool_name: str, args: dict[str, Any]) -> HookInput:
        rc = self.rc
        return HookInput(event=event, run_id=rc.run_id, session_id=rc.session_id, cwd=str(rc.workspace.root),
                         tool_name=tool_name, tool_input=dict(args), context=rc)

    async def _on_approval_created(self, rec, publish, perm, preview: str | None = None) -> None:
        publish(E.APPROVAL_REQUEST, {**rec.to_dict(), "analysis": perm.analysis, "effect": str(perm.effect),
                                     "preview": preview[:8000] if preview else None})
        await self._set_status("waiting_human", f"等待审批 {rec.id}")

    async def _set_status(self, to: str, reason: str) -> None:
        if self.rc.set_status is not None:
            await self.rc.set_status(to, reason)

    def _persist_grants(self) -> None:
        self._persist_session_meta(grants=[str(g) for g in self.rc.grants])

    def _persist_session_meta(self, **kv: Any) -> None:
        rc = self.rc
        s = rc.store.get_session(rc.session_id)
        if s is not None:
            rc.store.update_session(rc.session_id, meta={**s.meta, **kv})

    @staticmethod
    def _result_msg(call: ToolCall, result: ToolResult, status: str, duration_ms: int, snapshot: str | None) -> Message:
        details = dict(result.details or {})
        details.update(status=status, duration_ms=duration_ms)
        if snapshot:
            details["snapshot"] = snapshot
        msg = tool_result_message(call, result.content, is_error=result.is_error, details=details)
        msg.ts = now()
        return msg

    @staticmethod
    def _end_data(call: ToolCall, msg: Message) -> dict[str, Any]:
        d = msg.details or {}
        return {"call_id": call.id, "name": call.name, "status": d.get("status"), "is_error": msg.is_error,
                "content": clip(msg.text, 4000), "details": d, "duration_ms": d.get("duration_ms"), "message": msg.to_dict()}
