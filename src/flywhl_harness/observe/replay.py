"""回放：确定性回放（调试 / 回归）与反事实回放（从第 k 步分叉，评测新模型或新提示词）。

确定性回放
----------
模型输出从轨迹中读取（ReplayProvider），工具结果也从轨迹中读取（RecordedTool）——
**不调用模型、不执行任何副作用**。如果 harness 代码没有改变，回放应当得到完全相同的工具序列与最终答案；
一旦不同，ReplayProvider 记录的“上下文指纹不一致”的第一个位置就是分叉点
（例如改了系统提示词、工具描述、截断规则、压缩策略……）。

反事实回放（fork）
------------------
前 k 次模型调用使用录制结果，从第 k 次开始换成新的 provider；工作区复制到临时目录，
EXTERNAL 级工具一律切换为 dry-run——**分叉评测绝不能真的去发通知、改生产数据**。
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, AsyncIterator

from ..cancel import CancelToken
from ..config import HarnessConfig
from ..llm.base import ModelRequest, Provider, StreamEvent
from ..llm.replay import ReplayProvider
from ..runtime.store import Store
from ..runtime.workspace import IGNORED_DIRS
from ..tools.base import Effect, Tool, ToolContext, ToolResult
from ..tools.registry import ToolRegistry
from ..types import Message
from .trace import export_trajectory


class RecordedTool(Tool):
    """按 call_id 返回录制的工具结果；没有录制结果时（分叉之后）委托给 fallback 或 dry-run。"""

    def __init__(self, spec: dict[str, Any], recorded: dict[str, Message], fallback: Tool | None, dry_run_external: bool):
        self.name = spec["name"]
        self.description = spec["description"]
        self.parameters = spec["parameters"]
        self.recorded = recorded
        self.fallback = fallback
        self.dry_run_external = dry_run_external
        self.effect = fallback.effect if fallback else Effect.READ
        self.interruptible = True

    def effect_for(self, args: dict[str, Any], workspace: str | None = None) -> Effect:
        return self.fallback.effect_for(args, workspace) if self.fallback else Effect.READ

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        rec = self.recorded.get(ctx.call_id)
        if rec is not None:
            return ToolResult(rec.text, rec.is_error, {**(rec.details or {}), "replayed": True})
        if self.fallback is None:
            return ToolResult.error(f"[回放] 没有 {self.name} 的录制结果，且无可用的真实工具")
        if self.dry_run_external and self.fallback.effect_for(args, str(ctx.workspace.root)) == Effect.EXTERNAL:
            return ToolResult(f"[dry-run] {self.fallback.describe_call(args)} —— 分叉评测中不执行外部副作用",
                              details={"dry_run": True})
        return await self.fallback.execute(args, ctx)


class HybridProvider(Provider):
    """前 switch_at 次调用走录制结果，之后切换到 live provider。"""

    name = "hybrid"

    def __init__(self, replay: ReplayProvider, live: Provider | None, switch_at: int | None):
        self.replay, self.live, self.switch_at = replay, live, switch_at
        self.calls = 0

    def model_info(self, model: str):
        return (self.live or self.replay).model_info(model)

    async def stream(self, req: ModelRequest, cancel: CancelToken) -> AsyncIterator[StreamEvent]:
        if req.purpose != "compaction":
            self.calls += 1
        use_live = self.live is not None and self.switch_at is not None and self.calls > self.switch_at
        src = self.live if use_live else self.replay
        async for ev in src.stream(req, cancel):  # type: ignore[union-attr]
            yield ev


@dataclass
class ReplayReport:
    original_run_id: str
    replay_run_id: str
    mode: str
    status: str
    original_tools: list[str]
    replay_tools: list[str]
    same_tool_sequence: bool
    same_final_text: bool
    divergence: dict[str, Any] | None
    final_text: str
    workspace: str
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _copy_workspace(src: str) -> str:
    dst = tempfile.mkdtemp(prefix="flywhl-replay-")
    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns(*IGNORED_DIRS))
    return dst


async def replay_run(
    store: Store, run_id: str, *, config: HarnessConfig | None = None, live: Provider | None = None,
    live_model: str | None = None, fork_at: int | None = None, base_tools: ToolRegistry | None = None,
) -> ReplayReport:
    """回放一次 Run。``fork_at=None`` 为确定性回放；否则从第 fork_at 次模型调用起切换到 live provider。"""
    from ..runtime.manager import Harness

    traj = export_trajectory(store, run_id, include_children=False)
    msgs = [Message.from_dict(m) for m in traj["messages"]]
    prompt = next((m.text for m in msgs if m.role == "user" and m.kind == "prompt"), traj["run"]["prompt"] or "")
    recorded_tools = {m.tool_call_id: m for m in msgs if m.role == "tool" and m.tool_call_id}
    assistant = [m for m in msgs if m.role == "assistant" and m.stop_reason not in ("error", "aborted")]
    fps = traj["fingerprints"] if fork_at is None else traj["fingerprints"][:fork_at]

    ws = _copy_workspace(traj["session"]["workspace"])
    cfg = replace(config or HarnessConfig(), workspace=ws, data_dir=str(Path(ws) / ".flywhl"))
    cfg.permissions = replace(cfg.permissions, on_ask="allow")  # 回放不需要再次人工审批
    cfg.snapshots = False
    base = base_tools or ToolRegistry()
    specs = {t.name: t for t in base}
    registry = ToolRegistry()
    for name in traj["session"]["tools"] or []:
        real = specs.get(name)
        spec = real.spec() if real else {"name": name, "description": "", "parameters": {"type": "object"}}
        registry.register(RecordedTool(spec, recorded_tools if fork_at is None else
                                       {k: v for k, v in recorded_tools.items() if _call_index(assistant, k) < (fork_at or 0)},
                                       real, dry_run_external=True))
    rp = ReplayProvider(assistant, fps)
    provider = HybridProvider(rp, live, fork_at)
    model = live_model or traj["session"]["model"] or "replay"
    h = Harness(cfg, provider=provider, model=model, tools=registry, store=Store(":memory:"))
    s = h.create_session("回放", workspace=ws, system_prompt=traj["session"]["system_prompt"], mode="bypass")
    res = await h.run(prompt, session_id=s.id)
    replay_msgs = h.log(s.id).messages()
    orig_seq = [c.name for m in assistant for c in m.tool_calls]
    new_seq = [c.name for m in replay_msgs if m.role == "assistant" for c in m.tool_calls]
    orig_final = (traj["run"].get("result") or {}).get("text", "")
    notes = []
    if rp.divergence:
        notes.append(f"第 {rp.divergence['step']} 次模型调用的上下文与录制时不一致：harness 行为发生了变化")
    await h.aclose()
    return ReplayReport(run_id, res.run_id, "deterministic" if fork_at is None else f"fork@{fork_at}", res.status,
                        orig_seq, new_seq, orig_seq == new_seq, (res.text or "") == (orig_final or ""),
                        rp.divergence, res.text, ws, notes)


def _call_index(assistant: list[Message], call_id: str) -> int:
    for i, m in enumerate(assistant):
        if any(c.id == call_id for c in m.tool_calls):
            return i
    return 10**9
