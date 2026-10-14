"""评测：用可机器验证的产物判断成功，并如实报告不确定性。

三条纪律
--------
1. **以产物判定成功**：检查工作区里的文件、测试结果、结构化输出，而不是看模型说没说“完成了”；
2. **单独统计“过早停止”**：Run 以 completed 结束、但产物检查不通过——这是长任务最常见的失败模式；
3. **报告置信区间、做配对检验**：样本少时 80% 与 70% 可能没有显著差异。这里用 Wilson 区间（小样本友好）
   与精确 McNemar 检验（同一批用例上比较两个版本）。

同一用例重复多次（trials）可以估计非确定性带来的波动（pass@k 风格）。
"""

from __future__ import annotations

import math
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

CheckFn = Callable[[Path, Any], "bool | tuple[bool, str]"]


@dataclass
class EvalCase:
    id: str
    prompt: str
    check: CheckFn                                   # (工作区, RunResult) -> 是否通过[, 说明]
    setup: Callable[[Path], None] | None = None      # 准备工作区（写入初始文件等）
    tags: list[str] = field(default_factory=list)


@dataclass
class CaseResult:
    case_id: str
    trial: int
    passed: bool
    status: str
    note: str
    turns: int
    tokens: int
    cost: float


@dataclass
class EvalReport:
    results: list[CaseResult]

    @property
    def n(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(r.passed for r in self.results)

    @property
    def success_rate(self) -> float:
        return self.passed / self.n if self.n else 0.0

    @property
    def premature_stop_rate(self) -> float:
        """宣称完成（completed）但产物检查失败的比例。"""
        bad = sum(1 for r in self.results if r.status == "completed" and not r.passed)
        return bad / self.n if self.n else 0.0

    def summary(self) -> dict[str, Any]:
        lo, hi = wilson_interval(self.passed, self.n)
        return {
            "n": self.n, "passed": self.passed, "success_rate": round(self.success_rate, 4),
            "ci95": [round(lo, 4), round(hi, 4)], "premature_stop_rate": round(self.premature_stop_rate, 4),
            "avg_turns": round(sum(r.turns for r in self.results) / max(self.n, 1), 2),
            "avg_tokens": round(sum(r.tokens for r in self.results) / max(self.n, 1), 1),
            "total_cost_usd": round(sum(r.cost for r in self.results), 6),
        }


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """二项比例的 Wilson 置信区间：样本量小、比例接近 0 或 1 时比正态近似可靠得多。"""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def mcnemar_exact(a: list[bool], b: list[bool]) -> dict[str, Any]:
    """配对比较两个版本在同一批用例上的结果：只看“结论不一致”的用例，做精确二项检验（双侧）。"""
    assert len(a) == len(b), "配对检验要求两组结果一一对应"
    b_only = sum(1 for x, y in zip(a, b) if not x and y)   # 新版通过、旧版失败
    a_only = sum(1 for x, y in zip(a, b) if x and not y)   # 旧版通过、新版失败
    n = a_only + b_only
    if n == 0:
        return {"a_only": 0, "b_only": 0, "p_value": 1.0}
    k = min(a_only, b_only)
    p = sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n
    return {"a_only": a_only, "b_only": b_only, "p_value": min(1.0, 2 * p)}


async def run_eval(cases: list[EvalCase], harness_factory: Callable[[Path], Awaitable[Any]], *, trials: int = 1,
                   keep_workspaces: bool = False) -> EvalReport:
    """对每个用例：新建临时工作区 → setup → 用工厂创建 harness 执行 → 检查产物。"""
    results: list[CaseResult] = []
    for case in cases:
        for t in range(trials):
            ws = Path(tempfile.mkdtemp(prefix=f"flywhl-eval-{case.id}-"))
            try:
                if case.setup:
                    case.setup(ws)
                h = await harness_factory(ws)
                if hasattr(h, "start"):
                    await h.start()  # 幂等：工厂里已经 start 过也没关系；否则 MCP 工具不会加载
                try:
                    res = await h.run(case.prompt)
                finally:
                    await h.aclose()
                out = case.check(ws, res)
                ok, note = (out if isinstance(out, tuple) else (bool(out), ""))
                u = res.usage or {}
                results.append(CaseResult(case.id, t, ok, res.status, note,
                                          sum(1 for e in res.events if e.type == "turn_start"),
                                          int(u.get("input_tokens", 0)) + int(u.get("output_tokens", 0)),
                                          float(u.get("cost_usd", 0.0))))
            finally:
                if not keep_workspaces:
                    shutil.rmtree(ws, ignore_errors=True)
    return EvalReport(results)
