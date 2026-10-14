"""Run 生命周期状态机。

::

    created ─► queued ─► running ─┬─► completed | failed | timed_out | budget_exhausted | loop_detected | incomplete
                  ▲               ├─► paused ─────────► queued（resume）
                  │               ├─► waiting_human ──► running（审批完成）
                  │               ├─► cancelling ─────► cancelled
                  │               └─► interrupted（Worker 崩溃，被恢复流程标记）─► queued（resume）
                  └──────────────── failed（retry：attempt + 1）

原则
----
- **显式状态、终态不可变**：不要用多个布尔字段拼状态；
- **迁移表驱动**：(from → to) 白名单之外的迁移一律拒绝；
- **CAS 原子迁移**：``WHERE status=? AND version=?``，取消与完成并发到达时只有一个生效；
- **每次迁移都产生事件**（由运行时发布 ``status`` 事件），审计可追溯“谁、何时、为何”；
- **语义不合并**：paused（主动挂起）、waiting_human（等审批）、interrupted（崩溃）恢复条件各不相同。
"""

from __future__ import annotations

from enum import StrEnum

from .store import RunRecord, Store


class RunStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_HUMAN = "waiting_human"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BUDGET_EXHAUSTED = "budget_exhausted"
    LOOP_DETECTED = "loop_detected"
    INCOMPLETE = "incomplete"          # Stop 验收多次未通过：如实上报，绝不伪装成 completed
    INTERRUPTED = "interrupted"        # 心跳超时被判定为 Worker 崩溃


S = RunStatus
TERMINAL = frozenset({S.CANCELLED, S.COMPLETED, S.FAILED, S.TIMED_OUT, S.BUDGET_EXHAUSTED, S.LOOP_DETECTED, S.INCOMPLETE})
ACTIVE = frozenset({S.RUNNING, S.WAITING_HUMAN, S.CANCELLING})
_ENDINGS = {S.COMPLETED, S.FAILED, S.TIMED_OUT, S.BUDGET_EXHAUSTED, S.LOOP_DETECTED, S.INCOMPLETE}

TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    S.CREATED: frozenset({S.QUEUED, S.CANCELLED}),
    S.QUEUED: frozenset({S.RUNNING, S.CANCELLED}),
    S.RUNNING: frozenset({S.PAUSED, S.WAITING_HUMAN, S.CANCELLING, S.INTERRUPTED, *_ENDINGS}),
    S.WAITING_HUMAN: frozenset({S.RUNNING, S.CANCELLING, S.CANCELLED, S.INTERRUPTED, S.FAILED}),
    S.PAUSED: frozenset({S.QUEUED, S.CANCELLED}),
    S.CANCELLING: frozenset({S.CANCELLED, S.INTERRUPTED}),
    S.INTERRUPTED: frozenset({S.QUEUED, S.CANCELLED, S.FAILED}),
    S.FAILED: frozenset({S.QUEUED}),  # 重试：新的 attempt
    S.CANCELLED: frozenset(),
    S.COMPLETED: frozenset(),
    S.TIMED_OUT: frozenset(),
    S.BUDGET_EXHAUSTED: frozenset(),
    S.LOOP_DETECTED: frozenset(),
    S.INCOMPLETE: frozenset(),
}


class IllegalTransition(Exception):
    pass


class TransitionConflict(Exception):
    """CAS 失败：状态在读取之后被别人改了（并发取消、另一个 Worker 抢占……）。"""


class RunStateMachine:
    def __init__(self, store: Store):
        self.store = store

    def transition(
        self, run_id: str, to: RunStatus | str, *, expect: set[RunStatus] | frozenset[RunStatus] | None = None,
        reason: str = "", retries: int = 3, expect_version: int | None = None, **fields,
    ) -> tuple[RunRecord, RunRecord]:
        """执行一次状态迁移，返回 (迁移前, 迁移后)。

        ``expect`` 限定“只有当前处于这些状态时才迁移”；CAS 冲突时重读并重试（最多 retries 次），
        若重读后的状态已不允许该迁移，则抛出 IllegalTransition——调用方据此得到“状态已变更”的明确答复。

        ``expect_version`` 把迁移钉在调用方 **看到的那个版本** 上，不重读重试：用于“基于某次观察做出的决定”
        （例如崩溃接管：扫描时心跳已过期，但迁移前别的 Worker 可能已经接管并推进了版本——此时必须放弃）。
        """
        to = RunStatus(to)
        for _ in range(1 if expect_version is not None else retries):
            cur = self.store.get_run(run_id)
            if cur is None:
                raise KeyError(f"Run 不存在：{run_id}")
            if expect_version is not None and cur.version != expect_version:
                raise TransitionConflict(f"{run_id}: 版本已从 {expect_version} 变为 {cur.version}，放弃基于旧观察的迁移")
            frm = RunStatus(cur.status)
            if expect is not None and frm not in expect:
                raise IllegalTransition(f"{run_id}: 当前状态 {frm} 不在预期 {sorted(expect)} 中，拒绝迁移到 {to}")
            if to not in TRANSITIONS[frm]:
                raise IllegalTransition(f"{run_id}: 非法迁移 {frm} → {to}")
            extra = dict(fields)
            if reason:
                extra["status_reason"] = reason
            if to == S.QUEUED and frm == S.FAILED:
                extra["attempt"] = cur.attempt + 1
            if self.store.cas_run(run_id, frm, cur.version, to, **extra):
                return cur, self.store.get_run(run_id)  # type: ignore[return-value]
        raise TransitionConflict(f"{run_id}: 状态迁移到 {to} 时连续发生并发冲突")

    @staticmethod
    def is_terminal(status: str) -> bool:
        return RunStatus(status) in TERMINAL
