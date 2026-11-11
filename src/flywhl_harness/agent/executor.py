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
from dataclasses import dataclass, replace
from typing import Any, AsyncIterator

from .._utils import clip, now, short_hash
from ..cancel import RunCancelled
from ..events import E, Event
from ..policy.approvals import OUTSIDE_BY
from ..policy.hooks import HookEvent, HookInput
from ..policy.permissions import Decision, PermissionResult, grant_for, strictest
from ..runtime.workspace import CredentialPath, PathEscape
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

    def parallel(self, workspace: str | None = None) -> bool:
        return self.tool is not None and self.error is None and self.tool.concurrency_safe \
            and self.tool.effect_in(self.args, workspace) == Effect.READ


class ToolExecutor:
    def __init__(self, rc: RunContext):
        self.rc = rc
        self.last_results: dict[str, Message] = {}

    @property
    def _root(self) -> str:
        # 副作用等级与权限层同一口径（见 Tool.effect_for）：bash 命令里工作区内的绝对路径不算越界
        return str(self.rc.workspace.root)

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
            if batches and p.parallel(self._root) and batches[-1][0].parallel(self._root):
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
            if note := (msg.details or {}).get("approver_note"):
                rc.remind(f"用户在审批时拒绝了 {p.call.name} 调用，给出的指示是：{note}")
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
        effect = tool.effect_in(p.args, self._root) if tool and not p.error else Effect.READ
        publish(E.TOOL_START, {
            "call_id": call.id, "name": call.name, "args": call.arguments, "effect": str(effect),
            "summary": tool.describe_call(p.args) if tool else call.name, "recovering": recovering,
        })

        def done(result: ToolResult, status: str, snapshot: str | None = None) -> Message:
            # tool_end 由 run_batch 在结果落盘后统一发布
            return self._result_msg(call, result, status, int((time.monotonic() - t0) * 1000), snapshot, effect)

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

        # ---- 会话能力位：联网关掉了就是“不存在”，不是“要审批”，所以在权限判定之前拒绝 ----
        if tool.egress and (denied := self._capability_denied(tool)) is not None:
            return done(ToolResult.error(denied), "denied")

        # ---- 权限 + PreToolUse hooks ----
        perm = self._evaluate(tool, args, publish)
        decision, reason = perm.decision, perm.reason
        hook_asked = False
        if rc.hooks.has(HookEvent.PRE_TOOL_USE):
            out = await rc.hooks.run(HookEvent.PRE_TOOL_USE, self._hook_input(HookEvent.PRE_TOOL_USE, call.name, args))
            for w in out.warnings:
                publish(E.NOTICE, {"level": "warn", "code": "hook_warning", "text": w})
            if out.updated_input is not None:
                new_args, errs = validate(tool.parameters, out.updated_input)
                if errs:
                    return done(ToolResult.error("PreToolUse hook 改写后的参数未通过校验：\n" + "\n".join(errs)), "invalid")
                args = new_args
                perm = self._evaluate(tool, args)  # 改写后必须重新判定
                decision, reason = perm.decision, perm.reason
            if out.decision in ("allow", "ask", "deny"):
                hook_asked = out.decision == "ask"      # 守卫要人确认：无人值守时也不按规则自动批准
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
            on_ask = getattr(rc.approvals, "on_ask", "wait")
            outside_rule, outside_why = None, ""
            if perm.human_only and on_ask != "wait":
                if on_ask == "deny" and not hook_asked:   # --yes 放行不了人工门；守卫要人确认时也不按模式放行
                    outside_rule, outside_why = rc.permissions.outside_approval(tool, args, perm, tainted=rc.tainted)
                if outside_rule is None:
                    why = f"（{outside_why}）" if outside_why else ""
                    return done(ToolResult.error(f"权限拒绝：{reason}。这类操作必须由人当场确认，无人值守时一律拒绝{why}。"
                                                 "请换一种在沙箱里能完成的方式，或向用户说明需要什么权限。"), "denied")
            preview = tool.preview(args, ctx)
            grant = str(grant_for(tool, args))      # “本会话始终允许”会添加的规则：审批卡片上展示，也是疲劳统计的键
            fatigue = getattr(rc.approvals, "fatigue", None)
            asked = time.monotonic()
            auto_rule, auto_why = None, ""
            if getattr(rc.approvals, "on_ask", "wait") == "deny":   # 无人值守：auto_approve 规则命中的自动批准
                auto_rule, auto_why = (None, "PreToolUse hook 要求确认，不自动批准") if hook_asked \
                    else rc.permissions.auto_approval(tool, args, perm, tainted=rc.tainted)
            dec = await rc.approvals.request(
                run_id=rc.run_id, session_id=rc.session_id, call_id=call.id, tool=tool.name, args=args,
                summary=tool.describe_call(args), risk=perm.risk, reason=reason, cancel=rc.cancel,
                on_created=lambda rec: self._on_approval_created(rec, publish, perm, preview,
                                                                 None if perm.human_only else grant, fatigue),
                auto_rule=str(auto_rule) if auto_rule else None, auto_why=auto_why,
                outside_rule=outside_rule.pattern if outside_rule else None,
            )
            await self._set_status("running", "审批已处理")
            publish(E.APPROVAL_RESOLVED, {"call_id": call.id, "approved": dec.approved, "by": dec.by,
                                          "reason": dec.reason, "remember": dec.remember})
            if dec.by == OUTSIDE_BY:
                publish(E.NOTICE, {"level": "warn", "code": "approve_outside",
                                   "text": f"沙箱外执行：{tool.describe_call(args)}（--approve-outside {outside_rule.pattern}）"})
            if fatigue is not None:
                for text in fatigue.observe(rc.session_id, approved=dec.approved, by=dec.by, key=grant, risk=perm.risk,
                                            waited=time.monotonic() - asked, remembered=dec.remember):
                    publish(E.NOTICE, {"level": "warn", "code": "approval_fatigue", "text": text})
            if not dec.approved:
                if dec.reason and not dec.by.startswith(("policy:", "system")):
                    # 人写的理由是用户的指示，不放进工具结果：系统提示词说工具结果是数据，模型会把它当不可信内容，不照做。
                    # 记在 details 里，落盘时转成 reminder：回放走同一条路，上下文不分叉
                    return done(ToolResult.error("用户拒绝了该操作。请按随后的用户指示调整方案。", approver_note=dec.reason), "rejected")
                why = f"：{dec.reason}" if dec.reason else ""
                return done(ToolResult.error(f"用户拒绝了该操作{why}。请调整方案（例如改用只读方式），或询问用户的意图。"), "rejected")
            if dec.edited_args:
                new_args, errs = validate(tool.parameters, dec.edited_args)
                if errs:
                    return done(ToolResult.error("审批人修改后的参数未通过校验：\n" + "\n".join(errs)), "invalid")
                args = new_args
                recheck = self._evaluate(tool, args)  # 防 TOCTOU
                if recheck.decision == Decision.DENY:
                    return done(ToolResult.error(f"修改后的参数被权限策略拒绝：{recheck.reason}"), "denied")
                if recheck.human_only and not perm.human_only:   # 审批卡上没写的事不算批准过
                    return done(ToolResult.error(f"修改后的参数要过人工门，需要重新申请：{recheck.reason}"), "denied")
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
                        # 审批人看到的是改写之前的参数：改写后的版本同样要过权限策略，命中拒绝（灾难性命令、凭证、deny 规则）就不执行
                        final = self._evaluate(tool, args)
                        if final.decision == Decision.DENY:
                            return done(ToolResult.error(f"PreToolUse hook 改写后的参数被权限策略拒绝：{final.reason}"), "denied")
                        if final.human_only:   # 人批准的是改写之前的那条命令
                            return done(ToolResult.error(f"PreToolUse hook 改写后的参数要过人工门，需要重新申请：{final.reason}"), "denied")
            ctx.human_approved = perm.human_only   # 过了人工门：人当场确认的，或者人用 --approve-outside 提前写明的
            if dec.remember and not perm.human_only:   # 这一次的确认只管这一次
                g = grant_for(tool, args)
                rc.grants.append(g)
                self._persist_grants()
                publish(E.NOTICE, {"level": "info", "code": "grant", "text": f"已添加会话级授权：{g}"})

        # ---- 最后一个安全点：审批/hooks 期间可能已经收到取消；副作用发出之前必须再检查一次 ----
        rc.cancel.raise_if_cancelled()

        # ---- 预写 intent（+ 写操作前快照）----
        effect = tool.effect_in(args, self._root)
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
        self._settle_web(call, result)

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
        foreign = tool.reads_workspace and not rc.workspace_trusted   # 没信任过的工作区：文件是仓库作者写的，不是用户写的
        if (result.tainted or tool.untrusted_for(args) or foreign) and not rc.tainted:
            rc.tainted = True
            self._persist_session_meta(tainted=True)  # 污点属于会话：暂停恢复、崩溃接管之后依然有效
            source = "没有信任过的工作区" if foreign else "不可信来源"
            publish(E.NOTICE, {"level": "info", "code": "tainted",
                               "text": f"{call.name} 的输出来自{source}：之后的副作用操作将需要人工确认"})
        msg = self._result_msg(call, result, status, exec_ms, snapshot, effect)
        if decision == Decision.ASK:
            msg.details["approval_wait_ms"] = approval_wait_ms  # type: ignore[index]
        return msg

    # ================================================================== 联网工具
    def _capability_denied(self, tool: Tool) -> str | None:
        from ..config import web_rank
        from ..runtime.capabilities import current_web

        cap = current_web(self.rc.store, self.rc.session_id)
        need = getattr(tool, "web_level", "search")
        if web_rank(cap) >= web_rank(need):
            return None
        now_ = "已关闭联网" if cap == "off" else "只允许联网搜索、不能读取网页"
        return (f"本会话{now_}，{tool.name} 不可用。联网能力要在开工前选择，开工后只能收紧：需要的话请告诉用户新建会话，"
                "或者用已有信息继续。")

    def _evaluate(self, tool: Tool, args: dict[str, Any], publish: Any = None) -> PermissionResult:
        """权限判定，再叠加联网工具自己的范围与外传检查（只收紧：deny 直接拒绝，ask 把放行改成要确认）。"""
        rc = self.rc
        perm = rc.permissions.evaluate(tool, args, tainted=rc.tainted, grants=rc.grants)
        pre = getattr(tool, "precheck", None) if tool.egress else None
        if pre is None or perm.decision == Decision.DENY:
            return perm
        verdict, why, code = pre(args, rc)
        if verdict == "deny":
            if code and publish is not None:   # 不带参数：被拦下的正是参数里疑似密钥的那段，通知不能再抄一遍
                publish(E.NOTICE, {"level": "warn", "code": code, "text": f"{tool.name} 调用被拦截：{why}"})
            return replace(perm, decision=Decision.DENY, reason=why)
        if verdict == "ask" and rc.tainted and getattr(rc.approvals, "on_ask", "wait") == "allow":
            # --yes 自动批准所有 ask。会话读过不可信内容后，没出现过的网址正是注入外传的通道，
            # 自动批准就等于 fetch_scope = public；没读过不可信内容时照常按 --yes 放行
            return replace(perm, decision=Decision.DENY,
                           reason=f"{why}。会话读取过不可信内容，--yes 不自动批准这类读取：请先 web_search 找到它，或请用户把网址发过来")
        if verdict == "ask" and perm.decision == Decision.ALLOW:
            return replace(perm, decision=Decision.ASK, reason=why)
        return perm

    def _settle_web(self, call: ToolCall, result: ToolResult) -> None:
        """联网工具的记账：按结果里的 ``details["web"]`` 计次数和费用，同时释放并行批里的占位。
        回放时结果来自录制，按同样的数据记账，预算走向与录制时一致。"""
        rc = self.rc
        rc.budget.root().web_inflight.pop(f"{rc.run_id}:{call.id}", None)
        web = (result.details or {}).get("web")
        if isinstance(web, dict) and (web.get("counted") or web.get("cost_usd")):
            rc.budget.charge_web(str(web.get("op") or "search"), counted=bool(web.get("counted")),
                                 cost_usd=float(web.get("cost_usd") or 0.0))

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
                retryable = tool.effect_in(args, self._root) == Effect.READ or tool.idempotent
                delay = 0.2 * 2 ** attempt
                left_now = rc.budget.deadline_left()
                if retryable and attempt < 3 and (left_now is None or left_now > delay + 1):
                    await rc.cancel.sleep(delay)
                    continue
                tried = f"共尝试 {attempt} 次" if retryable else "非幂等工具不自动重试"
                return ToolResult.error(f"工具暂时不可用（{tried}）：{e}"), "error"
            except PathEscape as e:
                hint = "" if isinstance(e, CredentialPath) else "只能操作工作区内的文件。"
                return ToolResult.error(f"{e}。{hint}"), "denied"
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
            if (r.details or {}).get("timed_out") and tool.effect_in(args, self._root) == Effect.EXTERNAL \
                    and not tool.idempotent:
                # 工具自己管理超时（bash）时也一样：外部命令（git push、curl -X POST）超时 ≠ 没发生
                return ToolResult(f"{r.content}\n[结果未知：命令可能已经对外部系统生效（幂等键 {ctx.idempotency_key}）。"
                                  "请先核实外部状态，不要直接重试。]", True, r.details, r.tainted), "unknown"
            return r, ("error" if r.is_error else "ok")
        if rc.cancel.cancelled:
            if tool.interruptible_in(args, self._root):
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
        if tool.effect_in(args, self._root) == Effect.EXTERNAL and not tool.idempotent:
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

    async def _on_approval_created(self, rec, publish, perm, preview: str | None = None, grant: str | None = None,
                                   fatigue: Any = None) -> None:
        publish(E.APPROVAL_REQUEST, {**rec.to_dict(), "analysis": perm.analysis, "effect": str(perm.effect),
                                     "preview": preview[:8000] if preview else None, "grant": grant,
                                     "approved_before": fatigue.approved_before(rec.session_id, grant)
                                     if fatigue is not None and grant else 0})
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
    def _result_msg(call: ToolCall, result: ToolResult, status: str, duration_ms: int, snapshot: str | None,
                    effect: Effect | None = None) -> Message:
        details = dict(result.details or {})
        details.update(status=status, duration_ms=duration_ms)
        if effect is not None:
            # 按参数判定的实际副作用等级（bash 的 ls 是只读）：落盘后界面重绘时仍能显示正确的等级
            details["effect"] = str(effect)
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
