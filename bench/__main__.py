"""命令入口，用法见 bench/README.md。要调用模型或执行任务的命令只在一次性容器里运行。"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import shutil
import subprocess
import sys
import time
import os
from pathlib import Path

import flywhl_harness

from . import q1, q2, q3, redact, rehearsal, report
from .common import PLAN, BudgetExceeded, load_plan, require_disposable
from .taskset import load_tasks, validate_all


def write_manifest(out: Path, plan: dict, *, rehearsing: bool = False) -> None:
    path = out / "manifest.json"
    if path.is_file():
        return
    out.mkdir(parents=True, exist_ok=True)
    bwrap = shutil.which("bwrap")
    version = subprocess.run([bwrap, "--version"], capture_output=True, text=True).stdout.strip() if bwrap else ""
    path.write_text(json.dumps({
        "plan_id": plan["plan_id"], "rehearsal": rehearsing, "harness_version": flywhl_harness.__version__,
        "commit": os.environ.get("BENCH_COMMIT", ""), "spec": os.environ.get("BENCH_SPEC", ""),
        "python": platform.python_version(), "platform": platform.platform(), "bubblewrap": version,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "q1_models": plan["q1"]["models"],
        "q2_model": plan["q2"]["model"], "q2_configs": plan["q2"]["configs"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def rehearse(plan: dict, out: Path, public: Path) -> None:
    """脚本化模型走完整流程。Q1 没有密钥，用例全部跳过，走的是产物目录这条路。"""
    write_manifest(out, plan, rehearsing=True)
    q1.run_q1(plan, out, trials=1, models=plan["q1"]["models"][:1])
    plan = {**plan, "q2": {**plan["q2"], "trials": 1}}
    asyncio.run(q2.run_q2(plan, out, provider_for=rehearsal.script_for, model="scripted"))
    print(json.dumps(asyncio.run(q3.run_q3(out)), ensure_ascii=False), flush=True)
    print(json.dumps({"redaction_hits": redact.publish(out, public)}, ensure_ascii=False), flush=True)
    print(report.render(report.build(public)), flush=True)


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m bench")
    p.add_argument("command", choices=["validate", "pilot", "q1", "q2", "q3", "publish", "rehearse", "all", "report"])
    p.add_argument("--out", type=Path, default=Path("/results/raw"))
    p.add_argument("--public", type=Path, default=Path("/results/public"))
    p.add_argument("--plan", type=Path, default=PLAN)
    p.add_argument("--model", action="append", help="只跑这几家（Q1、pilot）")
    p.add_argument("--trials", type=int, help="覆盖方案里的次数（Q1）")
    p.add_argument("--draft", action="store_true", help="report：允许有未归类的失败，只打印不写站点")
    p.add_argument("--write", type=Path, help="report：写进这个 docs 目录")
    args = p.parse_args()
    plan = load_plan(args.plan)
    if args.command == "report":
        rep = report.build(args.out)
        if args.write:
            print("\n".join(map(str, report.write_pages(rep, args.write))))
            return 0
        print(report.render(rep))
        if rep["unclassified"] and not args.draft:
            print(f"\n还有 {len(rep['unclassified'])} 个失败没有归类：", *rep["unclassified"], sep="\n", file=sys.stderr)
            return 1
        return 0
    if args.command == "publish":
        print(json.dumps(redact.publish(args.out, args.public), ensure_ascii=False))
        return 0
    require_disposable()
    try:
        if args.command == "validate":
            bad = validate_all()
            print(f"{len(load_tasks())} 个任务，{len(bad)} 个有问题", json.dumps(bad, ensure_ascii=False, indent=1), sep="\n")
            return 1 if bad else 0
        if args.command == "rehearse":
            rehearse(plan, args.out, args.public)
            return 0
        write_manifest(args.out, plan)
        if args.command == "pilot":       # 每家跑一遍，量出单价再定次数
            q1.run_q1(plan, args.out, trials=1, models=args.model)
        if args.command in ("q1", "all"):
            q1.run_q1(plan, args.out, trials=args.trials, models=args.model)
        if args.command in ("q2", "all"):
            asyncio.run(q2.run_q2(plan, args.out))
        if args.command in ("q3", "all"):
            print(json.dumps(asyncio.run(q3.run_q3(args.out)), ensure_ascii=False))
        if args.command == "all":
            print(json.dumps({"redaction_hits": redact.publish(args.out, args.public)}, ensure_ascii=False))
    except BudgetExceeded as e:
        print(f"预算用完，已经跑完的数据都在：{e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
