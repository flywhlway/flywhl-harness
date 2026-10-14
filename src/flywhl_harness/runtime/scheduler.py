"""DAG 调度器：多 Agent Swarm 的最小模型。

把一个大任务拆成带依赖的子任务（通常每个子任务是一个子代理），由 **确定性代码** 做拓扑调度——
LLM 负责拆解与执行，不参与调度决策。

- 入度表 + 就绪队列；Kahn 算法同时完成环检测；
- 全局并发上限 + 每租户并发上限（信号量），防止一个大任务挤占整个平台；**先拿租户名额、再拿全局名额**——
  反过来的话，排队等租户名额的任务会占着全局名额干等，别的租户反而跑不起来（队头阻塞）；
- 三种失败策略：``fail_fast``（取消在途、跳过其余）、``skip_downstream``（只跳过失败任务的下游）、
  ``continue``（下游照常执行，失败的上游以 ``UpstreamFailed`` 传入）；每个任务可单独配置重试次数；
- 取消令牌与任务完成一起等待：取消请求立即生效，而不是等到下一个任务结束；
- 结果合并时记录每个子任务的来源、尝试次数与耗时，便于归因与关键路径分析。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..cancel import CancelToken


class CycleError(ValueError):
    pass


@dataclass
class TaskSpec:
    id: str
    fn: Callable[[dict[str, Any]], Awaitable[Any]]   # 参数：上游任务的结果 {dep_id: value}
    deps: list[str] = field(default_factory=list)
    tenant: str = "default"
    retries: int = 0
    timeout: float | None = None
    priority: int = 0                                 # 越小越先调度


@dataclass(frozen=True)
class UpstreamFailed:
    """continue 策略下传给下游的“上游失败”信息：下游自己决定降级处理还是放弃。"""
    id: str
    status: str
    error: str | None


@dataclass
class TaskResult:
    id: str
    status: str = "pending"    # ok | failed | skipped | cancelled
    value: Any = None
    error: str | None = None
    attempts: int = 0
    started_at: float | None = None
    ended_at: float | None = None
    deps: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return (self.ended_at or 0) - (self.started_at or 0) if self.started_at else 0.0


def topo_order(tasks: list[TaskSpec]) -> list[str]:
    """Kahn 拓扑排序；存在环时抛出 CycleError 并指出涉及的任务。"""
    ids = {t.id for t in tasks}
    for t in tasks:
        missing = [d for d in t.deps if d not in ids]
        if missing:
            raise ValueError(f"任务 {t.id} 依赖不存在的任务 {missing}")
    indeg = {t.id: len(t.deps) for t in tasks}
    children: dict[str, list[str]] = {t.id: [] for t in tasks}
    for t in tasks:
        for d in t.deps:
            children[d].append(t.id)
    q = deque(sorted(i for i, n in indeg.items() if n == 0))
    order = []
    while q:
        cur = q.popleft()
        order.append(cur)
        for c in children[cur]:
            indeg[c] -= 1
            if indeg[c] == 0:
                q.append(c)
    if len(order) != len(tasks):
        raise CycleError(f"依赖存在环，涉及任务：{sorted(i for i, n in indeg.items() if n > 0)}")
    return order


class DagScheduler:
    def __init__(self, max_concurrency: int = 4, per_tenant: int = 2, failure_policy: str = "skip_downstream",
                 cancel: CancelToken | None = None, on_event: Callable[[str, TaskResult], None] | None = None):
        assert failure_policy in ("fail_fast", "skip_downstream", "continue")
        self.max_concurrency = max_concurrency
        self.per_tenant = per_tenant
        self.failure_policy = failure_policy
        self.cancel = cancel or CancelToken()
        self.on_event = on_event or (lambda kind, r: None)

    async def run(self, tasks: list[TaskSpec]) -> dict[str, TaskResult]:
        topo_order(tasks)  # 校验依赖与环
        spec = {t.id: t for t in tasks}
        results = {t.id: TaskResult(t.id, deps=list(t.deps)) for t in tasks}
        children: dict[str, list[str]] = {t.id: [] for t in tasks}
        indeg = {t.id: len(t.deps) for t in tasks}
        for t in tasks:
            for d in t.deps:
                children[d].append(t.id)
        ready = sorted((i for i, n in indeg.items() if n == 0), key=lambda i: (spec[i].priority, i))
        global_sem = asyncio.Semaphore(self.max_concurrency)
        tenant_sems: dict[str, asyncio.Semaphore] = {}
        running: dict[asyncio.Task, str] = {}
        aborted = False

        async def execute(tid: str) -> None:
            t = spec[tid]
            r = results[tid]
            sem_t = tenant_sems.setdefault(t.tenant, asyncio.Semaphore(self.per_tenant))
            async with sem_t, global_sem:  # 先租户、后全局：等租户名额时不占用全局名额
                r.started_at = time.monotonic()
                self.on_event("start", r)
                upstream = {d: results[d].value if results[d].status == "ok"
                            else UpstreamFailed(d, results[d].status, results[d].error) for d in t.deps}
                try:
                    for attempt in range(t.retries + 1):
                        r.attempts = attempt + 1
                        try:
                            coro = t.fn(upstream)
                            r.value = await (asyncio.wait_for(coro, t.timeout) if t.timeout else coro)
                            r.status, r.error = "ok", None
                            break
                        except asyncio.CancelledError:
                            r.status, r.error = "cancelled", "已取消"
                            raise
                        except Exception as e:  # noqa: BLE001
                            r.status, r.error = "failed", f"{type(e).__name__}: {e}"
                            if attempt < t.retries:
                                await asyncio.sleep(min(2.0, 0.1 * 2 ** attempt))
                finally:  # 被取消的任务也要记下结束时间：关键路径与耗时归因依赖它
                    r.ended_at = time.monotonic()
                    self.on_event("end", r)

        def skip_descendants(tid: str, why: str) -> None:
            stack = list(children[tid])
            while stack:
                c = stack.pop()
                if results[c].status == "pending":
                    results[c].status, results[c].error = "skipped", why
                    self.on_event("skip", results[c])
                    stack.extend(children[c])

        def abort() -> None:
            nonlocal aborted
            aborted = True
            for task in running:
                task.cancel()

        # 取消令牌与任务一起等待：取消请求立即生效，而不是等到下一个任务结束
        cancel_waiter = asyncio.ensure_future(self.cancel.wait())
        try:
            while ready or running:
                if self.cancel.cancelled and not aborted:
                    abort()
                while ready and not aborted:
                    tid = ready.pop(0)
                    if results[tid].status == "pending":
                        running[asyncio.create_task(execute(tid))] = tid
                if not running:
                    break
                waits = set(running) if aborted else {*running, cancel_waiter}  # 已中止就不再等取消信号，避免空转
                done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
                for task in done - {cancel_waiter}:
                    tid = running.pop(task)
                    if results[tid].status == "failed":
                        if self.failure_policy == "fail_fast" and not aborted:
                            abort()
                        elif self.failure_policy == "skip_downstream":
                            skip_descendants(tid, f"上游任务 {tid} 失败")
                            continue
                    if aborted:
                        continue
                    for c in children[tid]:  # continue 策略下，失败任务的下游照常放行
                        indeg[c] -= 1
                        if indeg[c] == 0 and results[c].status == "pending":
                            ready.append(c)
                    ready.sort(key=lambda i: (spec[i].priority, i))
        finally:
            cancel_waiter.cancel()
        for r in results.values():
            if r.status == "pending":
                r.status, r.error = ("cancelled", "调度被中止") if aborted else ("skipped", "上游未完成")
        return results


def critical_path(tasks: list[TaskSpec], results: dict[str, TaskResult]) -> tuple[list[str], float]:
    """按实际耗时计算关键路径（最长路径）：优化长任务总耗时时，应该先优化这条路径上的任务。"""
    order = topo_order(tasks)
    spec = {t.id: t for t in tasks}
    best: dict[str, tuple[float, list[str]]] = {}
    for tid in order:
        d = results[tid].duration if tid in results else 0.0
        prev = max((best[p] for p in spec[tid].deps), key=lambda x: x[0], default=(0.0, []))
        best[tid] = (prev[0] + d, prev[1] + [tid])
    total, path = max(best.values(), key=lambda x: x[0], default=(0.0, []))
    return path, total
