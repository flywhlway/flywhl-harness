"""07 · Swarm 最小模型：确定性 DAG 调度 + 并发配额 + 失败策略 + 关键路径。

运行：python examples/07_swarm_dag.py

场景：把“迁移一个仓库”拆成 8 个子任务。LLM 负责拆解与执行（这里用 sleep 模拟子代理耗时），
**调度决策由确定性代码完成**：拓扑顺序、全局并发 3、每租户并发 2、某个任务失败时只跳过它的下游。
"""

import asyncio
import random
import time

from _common import G, R, X, Y, title

from flywhl_harness.runtime.scheduler import DagScheduler, TaskSpec, critical_path

T0 = time.monotonic()


def agent_task(name: str, seconds: float, fail: bool = False):
    async def run(upstream):
        await asyncio.sleep(seconds)
        if fail:
            raise RuntimeError(f"{name}：测试仍有 2 个失败")
        return f"{name} 完成（基于 {sorted(upstream) or '无上游'}）"
    return run


async def main() -> None:
    title("07 Swarm DAG 调度")
    random.seed(3)
    tasks = [
        TaskSpec("扫描依赖", agent_task("扫描依赖", 0.4)),
        TaskSpec("升级 core", agent_task("升级 core", 0.8), deps=["扫描依赖"], tenant="team-a"),
        TaskSpec("升级 api", agent_task("升级 api", 0.6), deps=["扫描依赖"], tenant="team-a"),
        TaskSpec("升级 web", agent_task("升级 web", 0.7), deps=["扫描依赖"], tenant="team-b"),
        TaskSpec("修复 core 测试", agent_task("修复 core 测试", 0.5, fail=True), deps=["升级 core"], tenant="team-a", retries=1),
        TaskSpec("修复 api 测试", agent_task("修复 api 测试", 0.4), deps=["升级 api"], tenant="team-a"),
        TaskSpec("集成测试", agent_task("集成测试", 0.6), deps=["修复 core 测试", "修复 api 测试", "升级 web"]),
        TaskSpec("更新文档", agent_task("更新文档", 0.3), deps=["升级 web"], tenant="team-b"),
    ]

    def on_event(kind, r):
        t = time.monotonic() - T0
        mark = {"start": f"{Y}▶{X}", "end": (G + "✓" if r.status == "ok" else R + "✗") + X, "skip": f"{R}⊘{X}"}[kind]
        extra = f"（第 {r.attempts} 次尝试）" if kind == "end" and r.attempts > 1 else ""
        print(f"  {t:5.2f}s {mark} {r.id}{extra}{'：' + r.error if r.error and kind != 'start' else ''}")

    sched = DagScheduler(max_concurrency=3, per_tenant=2, failure_policy="skip_downstream", on_event=on_event)
    results = await sched.run(tasks)
    title("合并结果（带来源与耗时）")
    for r in results.values():
        print(f"  {r.id:<10} {r.status:<8} 尝试 {r.attempts}  耗时 {r.duration:.2f}s  {r.value or ''}")
    path, total = critical_path(tasks, {k: v for k, v in results.items() if v.status == "ok"})
    print(f"\n  关键路径（已完成部分）：{' → '.join(path)}，{total:.2f}s —— 想缩短总耗时，先优化这条路径")


if __name__ == "__main__":
    asyncio.run(main())
