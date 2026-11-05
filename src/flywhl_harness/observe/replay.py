"""回放：确定性回放（调试 / 回归）与反事实回放（从第 k 步分叉，评测新模型或新提示词）。

确定性回放
----------
模型输出从轨迹中读取（ReplayProvider），工具结果也从轨迹中读取（RecordedTool）——
**不调用模型、不执行任何副作用**。如果 harness 代码没有改变，回放应当得到完全相同的工具序列与最终答案；
一旦不同，ReplayProvider 记录的“上下文指纹不一致”的第一个位置就是分叉点
（例如改了系统提示词、工具描述、截断规则、压缩策略……）。

录制时从外部进来的东西都要从轨迹里取，否则分叉的就不是 harness，而是回放自己：

- 模型写的压缩摘要；
- 人的插话与排队消息、暂停或崩溃恢复时的恢复说明，在录制时出现的位置重新注入；
- 验收命令的结果：工作区是终态，重跑会直接通过，所以回放不执行它，录制的验收反馈按原位置注入；
- 同一会话里前几次 Run 的历史，以及当时的设置（``run.config["replay"]``）。工作区可以已经不存在：工具结果都来自录制。

录制时被取消或崩溃的 Run 走不到结局，轨迹耗尽后回放以 failed 结束，这是预期结果，判定看 ``ReplayReport.deterministic``。

反事实回放（fork）
------------------
前 k 次模型调用使用录制结果，从第 k 次开始换成新的 provider；工作区复制到临时目录，
EXTERNAL 级工具一律切换为 dry-run——**分叉评测绝不能真的去发通知、改生产数据**。
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, AsyncIterator

from ..cancel import CancelToken
from ..config import BudgetConfig, ContextConfig, HarnessConfig, LoopGuardConfig
from ..llm.base import ModelRequest, Provider, StreamEvent
from ..llm.replay import ReplayProvider
from ..runtime.store import Store
from ..runtime.workspace import IGNORED_DIRS
from ..tools import default_tools
from ..tools.base import Effect, Tool, ToolContext, ToolResult
from ..tools.registry import ToolRegistry
from ..types import Message, MsgKind
from .trace import export_trajectory

# 录制时从外部进来的用户消息。其余注入（预算、循环、计划提醒）是 harness 自己算出来的，回放时重算，正是要检查的部分
EXTERNAL_INPUTS = frozenset({MsgKind.STEER, MsgKind.FOLLOW_UP, MsgKind.RESUME_NOTE})
SECTIONS = {"context": ContextConfig, "budget": BudgetConfig, "loop_guard": LoopGuardConfig}


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
        return self.fallback.effect_in(args, workspace) if self.fallback else Effect.READ

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        rec = self.recorded.get(ctx.call_id)
        if rec is not None:
            return ToolResult(rec.text, rec.is_error, {**(rec.details or {}), "replayed": True})
        if self.fallback is None:
            return ToolResult.error(f"[回放] 没有 {self.name} 的录制结果，且无可用的真实工具")
        if self.dry_run_external and self.fallback.effect_in(args, str(ctx.workspace.root)) == Effect.EXTERNAL:
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
    original_status: str = ""

    @property
    def deterministic(self) -> bool:
        """上下文和工具序列都和录制一致。录制时走到了结局的，状态和最终答案也要一致；取消、崩溃、验收多次未过的走不到同样的结局。"""
        if self.divergence is not None or not self.same_tool_sequence:
            return False
        return self.original_status not in ("completed", "budget_exhausted") or \
            (self.status == self.original_status and self.same_final_text)

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "deterministic": self.deterministic}


def _copy_workspace(src: str) -> str:
    dst = tempfile.mkdtemp(prefix="flywhl-replay-")
    if Path(src).is_dir():      # 工作区删掉了也能回放：工具结果都来自录制，分叉评测才需要真实文件
        shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns(*IGNORED_DIRS))
    return dst


def _recorded_config(settings: dict[str, Any]) -> HarnessConfig:
    """按录制时的设置重建配置。字段是白名单，轨迹来自旧版本或者多了字段都不影响。"""
    cfg = HarnessConfig()
    for name, cls in SECTIONS.items():
        known = {f.name for f in fields(cls)}
        setattr(cfg, name, cls(**{k: v for k, v in (settings.get(name) or {}).items() if k in known}))
    for name in ("max_stop_blocks", "verify_todos", "enable_demo_tools"):
        if name in settings:
            setattr(cfg, name, settings[name])
    cfg.permissions = replace(cfg.permissions, sandbox=settings.get("sandbox", cfg.permissions.sandbox),
                              sandbox_network=settings.get("sandbox_network", cfg.permissions.sandbox_network))
    return cfg


def _external_inputs(msgs: list[Message], kinds: frozenset[str]) -> dict[int, list[str]]:
    """{第几条有效回复之后: [这期间进来的外部输入]}。-1 表示第一次调用模型之前。"""
    out: dict[int, list[str]] = {}
    seen = -1
    for m in msgs:
        if m.role == "assistant" and m.stop_reason not in ("error", "aborted"):
            seen += 1
        elif m.role == "user" and m.kind in kinds:
            out.setdefault(seen, []).append(m.text)
    return out


async def replay_run(
    store: Store, run_id: str, *, config: HarnessConfig | None = None, live: Provider | None = None,
    live_model: str | None = None, fork_at: int | None = None, base_tools: ToolRegistry | None = None,
) -> ReplayReport:
    """回放一次 Run。``fork_at=None`` 为确定性回放；否则从第 fork_at 次模型调用起切换到 live provider。"""
    return await replay_trajectory(export_trajectory(store, run_id, include_children=False), config=config, live=live,
                                   live_model=live_model, fork_at=fork_at, base_tools=base_tools)


async def replay_trajectory(
    traj: dict[str, Any], *, config: HarnessConfig | None = None, live: Provider | None = None,
    live_model: str | None = None, fork_at: int | None = None, base_tools: ToolRegistry | None = None,
) -> ReplayReport:
    """从导出的轨迹回放，不需要原来的数据库。不给 ``config`` 时用轨迹里记下的设置；给了就以它为准，用来看改设置会不会分叉。"""
    from ..runtime.manager import Harness

    run_id = traj["run"]["id"]
    recorded = traj["run"].get("config") or {}
    settings = recorded.get("replay") or {}
    msgs = [Message.from_dict(m) for m in traj["messages"]]
    prompt = next((m.text for m in msgs if m.role == "user" and m.kind == "prompt"), traj["run"]["prompt"] or "")
    recorded_tools = {m.tool_call_id: m for m in msgs if m.role == "tool" and m.tool_call_id}
    assistant = [m for m in msgs if m.role == "assistant" and m.stop_reason not in ("error", "aborted")]
    fps = traj["fingerprints"] if fork_at is None else traj["fingerprints"][:fork_at]

    ws = _copy_workspace(traj["session"]["workspace"])
    base_cfg = config or _recorded_config(settings)
    cfg = replace(base_cfg, workspace=ws, data_dir=str(Path(ws) / ".flywhl"))
    cfg.context = replace(cfg.context, context_window=cfg.context.context_window or settings.get("context_window"))
    cfg.permissions = replace(cfg.permissions, on_ask="allow")  # 回放不需要再次人工审批
    cfg.snapshots = False
    verified = bool(cfg.verify_command or settings.get("verify"))
    if fork_at is None:
        cfg.verify_command = None                               # 结果从录制里取，见模块说明
        if recorded.get("mode") == "plan":                      # 回放的模式是 bypass，规划模式不验收计划这一条要自己保留
            cfg.verify_todos = False
    base = base_tools if base_tools is not None else default_tools(cfg, Path(cfg.data_dir))
    specs = {t.name: t for t in base}
    registry = ToolRegistry()
    for name in traj["session"]["tools"] or []:
        real = specs.get(name)
        spec = real.spec() if real else {"name": name, "description": "", "parameters": {"type": "object"}}
        registry.register(RecordedTool(spec, recorded_tools if fork_at is None else
                                       {k: v for k, v in recorded_tools.items() if _call_index(assistant, k) < (fork_at or 0)},
                                       real, dry_run_external=True))
    kinds = EXTERNAL_INPUTS | ({MsgKind.STOP_FEEDBACK} if verified and fork_at is None else frozenset())
    inputs = _external_inputs(msgs, kinds)
    rp = ReplayProvider(assistant, fps, summaries=[c["summary"] for c in traj.get("compactions") or []])
    provider = HybridProvider(rp, live, fork_at)
    model = live_model or traj["session"]["model"] or "replay"
    h = Harness(cfg, provider=provider, model=model, tools=registry, store=Store(":memory:"))
    seeded = sum(1 for e in traj.get("history") or [] if e["kind"] == "message")
    try:
        s = h.create_session("回放", workspace=ws, system_prompt=traj["session"]["system_prompt"], mode="bypass")
        for e in traj.get("history") or []:
            h.store.append_entry(s.id, e["kind"], e["payload"], entry_id=e["id"])
        if settings.get("clear_watermark"):
            h.store.update_session(s.id, meta={**h.store.get_session(s.id).meta, "clear_watermark": settings["clear_watermark"]})
        run = await h.start_run(s.id, prompt)

        def feed(turn: int) -> None:
            if fork_at is None or turn < fork_at:
                for text in inputs.get(turn, []):
                    h.steer(run.id, text)

        rp.on_turn = feed
        feed(-1)
        rec = await h.wait(run.id)
        replay_msgs = h.log(s.id).messages()[seeded:]
    finally:
        await h.aclose()
    orig_seq = [c.name for m in assistant for c in m.tool_calls]
    new_seq = [c.name for m in replay_msgs if m.role == "assistant" for c in m.tool_calls]
    orig_final = (traj["run"].get("result") or {}).get("text", "")
    final = (rec.result or {}).get("text") or ""
    notes = []
    if rp.divergence:
        notes.append(f"第 {rp.divergence['step']} 次模型调用的上下文与录制时不一致：harness 行为发生了变化")
    return ReplayReport(run_id, rec.id, "deterministic" if fork_at is None else f"fork@{fork_at}", rec.status,
                        orig_seq, new_seq, orig_seq == new_seq, final == (orig_final or ""),
                        rp.divergence, final, ws, notes, traj["run"].get("status", ""))


def _call_index(assistant: list[Message], call_id: str) -> int:
    for i, m in enumerate(assistant):
        if any(c.id == call_id for c in m.tool_calls):
            return i
    return 10**9
