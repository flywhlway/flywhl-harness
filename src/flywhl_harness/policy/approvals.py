"""人审（Human-in-the-loop）：审批请求是持久化对象，Run 进入 waiting_human 做持久等待。

设计要点
--------
- **审批真实动作**：审批对象携带工具名与 *实际参数*（以及 diff 预览），而不是模型写的自我描述；
- **CAS 决定一次**：批准、拒绝、超时、取消并发到达时，只有第一个生效（store.resolve_approval）；
- **超时默认拒绝**：拒绝理由作为 is_error 的工具结果回给模型，让它调整方案；
- **防 TOCTOU**：批准时可修改参数，修改后的参数要 **重新做 schema 校验与权限判定**（在执行层完成）；
- **防审批疲劳**：低风险自动放行；“本会话总是允许”生成一条窄粒度的会话级授权规则。
- **无人值守**：``on_ask=allow|deny`` 用于 CI / 批处理，此时不创建审批对象。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .._utils import new_id, now
from ..cancel import CancelToken, RunCancelled
from ..runtime.store import ApprovalRecord, Store


@dataclass
class ApprovalDecision:
    approved: bool
    by: str = "user"
    reason: str = ""
    edited_args: dict[str, Any] | None = None
    remember: bool = False   # 本会话内对同类操作不再询问


Resolver = Callable[[ApprovalRecord], Awaitable[ApprovalDecision]]


class ApprovalBroker:
    def __init__(self, store: Store, *, on_ask: str = "wait", timeout: float = 600.0, resolver: Resolver | None = None):
        self.store = store
        self.on_ask = on_ask
        self.timeout = timeout
        self.resolver = resolver          # 交互式 CLI 在终端里问用户；Web UI 则通过 API 调用 resolve()
        self._waiters: dict[str, asyncio.Future[ApprovalDecision]] = {}

    async def request(
        self, *, run_id: str, session_id: str, call_id: str, tool: str, args: dict[str, Any], summary: str,
        risk: str, reason: str, cancel: CancelToken,
        on_created: Callable[[ApprovalRecord], Awaitable[None]] | None = None,
    ) -> ApprovalDecision:
        if self.on_ask == "allow":
            return ApprovalDecision(True, by="policy:auto_allow", reason="无人值守模式自动放行")
        if self.on_ask == "deny":
            return ApprovalDecision(False, by="policy:auto_deny", reason="无人值守模式下，需要审批的操作一律拒绝")
        rec = self.store.create_approval(ApprovalRecord(
            id=new_id("ap_"), run_id=run_id, session_id=session_id, call_id=call_id, tool=tool, args=args,
            summary=summary, risk=risk, reason=reason, status="pending", expires_at=now() + self.timeout,
        ))
        fut: asyncio.Future[ApprovalDecision] = asyncio.get_running_loop().create_future()
        self._waiters[rec.id] = fut
        resolver_task = None
        try:
            if on_created is not None:
                await on_created(rec)
            if self.resolver is not None:
                async def ask() -> None:
                    try:
                        self.resolve(rec.id, await self.resolver(rec))  # type: ignore[misc]
                    except Exception as e:  # 终端被关闭等：按拒绝处理
                        self.resolve(rec.id, ApprovalDecision(False, by="system", reason=f"审批交互失败：{e}"))
                resolver_task = asyncio.create_task(ask())
            try:
                return await cancel.guard(asyncio.wait_for(asyncio.shield(fut), self.timeout))
            except asyncio.TimeoutError:
                if self.store.resolve_approval(rec.id, "expired", decided_by="system:timeout", reason="审批超时"):
                    return ApprovalDecision(False, by="system:timeout", reason=f"审批在 {self.timeout:.0f}s 内未得到答复，默认拒绝")
                return self._from_record(rec.id)  # 与超时同时到达的人工决定胜出
        except RunCancelled:
            self.store.resolve_approval(rec.id, "expired", decided_by="system:cancel", reason="运行已取消")
            raise
        finally:
            self._waiters.pop(rec.id, None)
            if resolver_task is not None and not resolver_task.done():
                resolver_task.cancel()

    def resolve(self, approval_id: str, decision: ApprovalDecision) -> bool:
        """外部（Web UI / CLI）提交审批决定。返回 False 表示已被处理过（CAS 失败）。"""
        status = "approved" if decision.approved else "rejected"
        ok = self.store.resolve_approval(approval_id, status, decided_by=decision.by, reason=decision.reason,
                                         edited_args=decision.edited_args, remember=decision.remember)
        fut = self._waiters.get(approval_id)
        if ok and fut is not None and not fut.done():
            fut.set_result(decision)
        return ok

    def _from_record(self, approval_id: str) -> ApprovalDecision:
        r = self.store.get_approval(approval_id)
        if r is None:
            return ApprovalDecision(False, by="system", reason="审批记录丢失")
        return ApprovalDecision(r.status == "approved", by=r.decided_by or "unknown", reason=r.decision_reason or "",
                                edited_args=r.edited_args, remember=r.remember)
