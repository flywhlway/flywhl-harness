"""Q3：把 Q1、Q2 的全部轨迹回放一遍。不调用模型、不执行命令，只统计分叉数。

子代理的轨迹嵌在父轨迹里，父轨迹回放时 ``task`` 工具的结果来自录制，不单独回放。
"""

from __future__ import annotations

import shutil
import traceback
from pathlib import Path
from typing import Any, Iterator

from flywhl_harness.observe.replay import replay_trajectory

from .common import append_jsonl, read_jsonl


def trajectories(out: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    for path in sorted((out / "q1").glob("*/trial-*/trajectories.jsonl")) if (out / "q1").is_dir() else []:
        src = str(path.parent.relative_to(out))
        for row in read_jsonl(path):
            yield f"{src}/{row['nodeid']}", row["trajectory"]
    for row in read_jsonl(out / "q2" / "trajectories.jsonl"):
        yield f"q2/{row['task']}/{row['config']}/{row['trial']}", row["trajectory"]


async def replay_one(source: str, traj: dict[str, Any]) -> dict[str, Any]:
    row = {"source": source, "run_id": traj["run"]["id"], "original_status": traj["run"].get("status")}
    try:
        rep = await replay_trajectory(traj)
    except Exception:      # 回放崩了也是结论，不能让一条轨迹挡住全部统计
        return {**row, "deterministic": False, "error": traceback.format_exc()[-2000:]}
    shutil.rmtree(rep.workspace, ignore_errors=True)
    return {**row, "deterministic": rep.deterministic, "status": rep.status, "divergence": rep.divergence,
            "same_tool_sequence": rep.same_tool_sequence, "same_final_text": rep.same_final_text, "notes": rep.notes}


async def run_q3(out: Path) -> dict[str, int]:
    path = out / "q3" / "replays.jsonl"
    seen = {r["source"] + r["run_id"] for r in read_jsonl(path)}
    for source, traj in trajectories(out):
        if source + traj["run"]["id"] not in seen:
            append_jsonl(path, [await replay_one(source, traj)])
    rows = list(read_jsonl(path))
    return {"trajectories": len(rows), "divergent": sum(1 for r in rows if not r["deterministic"])}
