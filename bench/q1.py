"""Q1：通用场景乘以供应商，每组跑若干次。每次是一遍 pytest，产物由 tests/live/conftest.py 写到试验目录。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .common import REPO, Ledger, read_jsonl, safe_name

TRIAL_TIMEOUT = 1800


def trial_cost(trial_dir: Path) -> float:
    return sum(float(r.get("cost_usd") or 0) for row in read_jsonl(trial_dir / "results.jsonl") for r in row.get("runs") or [])


def run_trial(model: str, scenarios: list[str], trial_dir: Path) -> dict[str, Any]:
    trial_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "FLYWHL_LIVE_MODEL": model, "FLYWHL_LIVE_ARTIFACTS": str(trial_dir),
           "FLYWHL_TEST_SANDBOX": "off",        # 已经在一次性容器里，不再套外层沙箱，外层会让被测的沙箱起不来
           "FLYWHL_TEST_REAL_SANDBOX": "1"}     # 被测代码用真沙箱，按真实默认值运行
    started = time.time()
    with open(trial_dir / "pytest.log", "w", encoding="utf-8") as log:
        try:
            code = subprocess.run([sys.executable, "-m", "pytest", "-m", "live", "-q", "--tb=short", "-p", "no:cacheprovider", *scenarios],
                                  cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                  timeout=TRIAL_TIMEOUT).returncode
        except subprocess.TimeoutExpired:
            code = -1
    done = {"exit_code": code, "seconds": round(time.time() - started, 1), "model": model}
    (trial_dir / "done.json").write_text(json.dumps(done), encoding="utf-8")
    return done


def run_q1(plan: dict[str, Any], out: Path, *, trials: int | None = None, models: list[str] | None = None) -> None:
    """跑完的试验有 done.json，中断后接着跑不重复花钱。"""
    ledger = Ledger.from_plan(plan, out)
    cfg = plan["q1"]
    for model in models or cfg["models"]:
        for n in range(1, (trials or cfg["trials"]) + 1):
            trial_dir = out / "q1" / safe_name(model) / f"trial-{n}"
            if (trial_dir / "done.json").is_file():
                continue
            ledger.check(model)
            done = run_trial(model, cfg["scenarios"], trial_dir)
            cost = trial_cost(trial_dir)
            ledger.charge(model, cost)
            print(f"Q1 {model} 第 {n} 次：退出码 {done['exit_code']}，{done['seconds']} 秒，${cost:.4f}", flush=True)
