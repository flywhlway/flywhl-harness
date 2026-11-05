"""彩排：不花钱地把整条流水线走一遍。

脚本化模型照着参考答案写文件、跑 solution.sh，工具、沙箱、检查脚本、轨迹、回放、脱敏、报告都是真的，只有模型是假的。
沙箱敏感的任务在 A 配置下应当失败，B 配置下应当通过：这证明这几道题确实碰得到沙箱的边界。
彩排的结果带 ``rehearsal`` 标记，报告拒绝把它写进站点。
"""

from __future__ import annotations

from flywhl_harness.llm.mock import ScriptedProvider

from .taskset import Task


def script_for(task: Task) -> ScriptedProvider:
    turns = []
    sol = task.dir / "solution"
    if sol.is_dir():
        for path in sorted(p for p in sol.rglob("*") if p.is_file()):
            rel = str(path.relative_to(sol))
            content = path.read_text(encoding="utf-8")
            turns.append({"tool_calls": [{"name": "bash", "args": {"command": f"mkdir -p \"$(dirname '{rel}')\" && touch '{rel}'"}}]})
            turns.append({"tool_calls": [{"name": "read", "args": {"path": rel}}]})
            turns.append({"tool_calls": [{"name": "write", "args": {"path": rel, "content": content}}]})
    script = task.dir / "solution.sh"
    if script.is_file():
        turns.append({"tool_calls": [{"name": "bash", "args": {"command": script.read_text(encoding="utf-8")}}]})
    return ScriptedProvider([*turns, "完成"], mode="sequential")
