#!/usr/bin/env python3
"""覆盖率棘轮：整体和关键模块不能比基线低超过容差。

不设绝对阈值：85% 这样的数字逼人补凑数的用例，棘轮只管“不能悄悄变差”。基线是
``docs/quality-audit/coverage-baseline.json``，只在 Ubuntu 的 ``coverage-audit`` 作业比较，
Linux 档沙箱的代码只有那里才算得上，macOS 档的分支由 macOS 上的系统沙箱用例执行。

    coverage json -o coverage.json
    python scripts/coverage_ratchet.py coverage.json            # 比较，低于基线时退出码 1
    python scripts/coverage_ratchet.py coverage.json --update   # 写基线，只升不降

基线经 PR 上调。要往下调时手改文件，理由写进 PR。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "docs" / "quality-audit" / "coverage-baseline.json"
TOLERANCE = 0.5     # 百分点。行号变动、并发时序会让数字抖动，比这更大的下降才算变差
CRITICAL = tuple(f"src/flywhl_harness/{m}" for m in (
    "policy/permissions.py", "policy/approvals.py", "policy/hooks.py",
    "tools/sandbox.py", "tools/egress.py", "tools/confine.py", "tools/shell.py", "tools/fs.py",
    "agent/executor.py", "runtime/manager.py",
))
OVERALL = "整体"


def read_current(report: dict[str, Any]) -> dict[str, float]:
    """从 ``coverage json`` 的报告里取整体和关键模块的百分比（语句加分支）。"""
    current = {OVERALL: float(report["totals"]["percent_covered"])}
    files = report["files"]
    for path in CRITICAL:
        if path in files:
            current[path] = float(files[path]["summary"]["percent_covered"])
    return current


def compare(baseline: dict[str, float], current: dict[str, float], tolerance: float = TOLERANCE) -> list[str]:
    """低于基线超过容差的项，每项一句话。基线里有、报告里没有的算失败：文件改名不能悄悄退出比较。"""
    problems = []
    for name, base in baseline.items():
        now = current.get(name)
        if now is None:
            problems.append(f"{name}：报告里没有这一项（基线 {base:.2f}%）")
        elif now < base - tolerance:
            problems.append(f"{name}：{now:.2f}%，基线 {base:.2f}%，低了 {base - now:.2f} 个百分点")
    return problems


def raised(baseline: dict[str, float], current: dict[str, float]) -> dict[str, float]:
    """新基线：每项取较高的值，只升不降。"""
    keys = dict.fromkeys([*baseline, *current])
    return {k: round(max(baseline.get(k, 0.0), current.get(k, 0.0)), 2) for k in keys}


def table(baseline: dict[str, float], current: dict[str, float]) -> str:
    rows = ["| 项 | 当前 | 基线 | 差 |", "|---|---:|---:|---:|"]
    for name, now in current.items():
        base = baseline.get(name)
        rows.append(f"| `{name}` | {now:.2f}% | " + (f"{base:.2f}% | {now - base:+.2f} |" if base is not None else "— | — |"))
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="覆盖率棘轮")
    ap.add_argument("report", type=Path, help="coverage json 的输出")
    ap.add_argument("--baseline", type=Path, default=BASELINE)
    ap.add_argument("--update", action="store_true", help="把基线上调到当前值，只升不降")
    args = ap.parse_args(argv)
    current = read_current(json.loads(args.report.read_text(encoding="utf-8")))
    baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline.exists() else {}
    if args.update:
        args.baseline.write_text(json.dumps(raised(baseline, current), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0
    print("## 覆盖率棘轮\n\n" + table(baseline, current) + "\n")
    if not baseline:
        print(f"基线不存在：{args.baseline}")
        return 1
    problems = compare(baseline, current)
    for p in problems:
        print(f"- 低于基线：{p}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
