# 28 The removable harness

*Summary. [Read the full chapter in Chinese](/advanced/evolving-harness).*

## The problem

Every harness component encodes a belief that the model cannot do something by itself. Models improve, and some of those beliefs expire. A plan reminder that helped one model can become noise for the next. Deleting by gut feeling is risky, because some components were never about the model at all.

## How it works

**Ask three questions of each component.** Who does it serve? Can its assumption be measured? Is the cost of a wrong deletion symmetric? The answers sort components into three layers.

| Layer | Examples in this repo | Trend as models improve | Risk of removing |
|---|---|---|---|
| Capability compensation | plan recitation (`ContextManager.todo_reminder`), Stop plan check (`AgentLoop._stop_gaps`), `LoopGuard.observe`, soft budget warnings, the `<rules>` block in `prompts.SYSTEM_PROMPT` | Loses value, may start to interfere | Success rate drops a few points |
| Efficiency | tool result offloading, old-result clearing, parallel tools, frozen system prompt | Tied to window size and pricing, not intelligence | Higher cost or latency |
| Governance | permissions, approvals, step journal and `reconcile`, CAS state machine, hard budget limit, shadow git, taint, catastrophic-command denial, event audit | Never loses value | Unauthorized actions, duplicate side effects, lost data |

Only the first layer can be absorbed by a stronger model. Governance serves the operator and the organization. A smarter model cannot decide that a command needs a human, and it cannot recover a crashed process.

A component that looks like compensation can be governance. When output is cut off by `max_tokens`, the loop fails the whole batch. That protects the rule that only complete arguments run, and no model strength changes it.

**Write the assumption down.** The chapter proposes an assumption register: component, assumption, evidence for introducing it, which models it applies to, metric, removal condition and switch. Conclusions are recorded per model, since a component can help one model and hurt another.

**Switches exist for some components, not all.** Examples: `verify_todos`, `verify_command`, `context.clear_old_tool_results`, `context.auto_compact`, `snapshots`, `budget.soft_ratio`, `parallel_tool_limit`, `load_agents_md`, `load_skills`. Two do not do what they suggest:

- `max_stop_blocks=0` does not disable Stop checks. The test is `rc.stop_blocks < rc.config.max_stop_blocks`, so with 0 any gap ends the run as `incomplete`.
- `subagent_max_depth=0` does not remove the `task` tool from the main agent. It only strips `task` from subagents at the deepest level.

Plan recitation (`every_turns=4`), the ping-pong window in `LoopGuard.observe` and individual `<rules>` lines have no switch yet. The chapter suggests adding them.

**Measure with an ablation.** `observe/evals.py` provides `run_eval` (fresh temporary workspace per case, pass/fail by artifact check), `EvalReport.summary()` (Wilson interval and `premature_stop_rate`) and `mcnemar_exact`, a paired test that uses only cases where the two variants disagree. Run the same cases with the component on and off, on a real model. Scripted models are deterministic, so an ablation against them says nothing. Rules from the chapter:

1. Only discordant cases count.
2. With `trials > 1`, repeats of one case are correlated, so aggregate per case before pairing.
3. Watch `premature_stop_rate` when removing Stop checks, and token cost for the component's own overhead.
4. Re-run per model.

`llm/replay.py` `context_fingerprint` helps here: removing a reminder changes the fingerprint, so old trajectories diverge at the first affected call instead of silently replaying.

**Keep the seams stable.** Replacing the loop should be cheap if everything depends on a few contracts: `Tool`, `Provider.stream()`, append-only session entries, the `Event` stream and `RunContext`. One seam is still closed: `Harness` builds `AgentLoop(rc)` directly (`runtime/manager.py`), so comparing two loops side by side would need an injectable factory. Compensation strategy should live in hooks and config, not in the core.

## Key code

| Where | What it does |
|---|---|
| `agent/context.py` `ContextManager.todo_reminder()` | Plan recitation, fixed at every 4 turns |
| `agent/loop.py` `AgentLoop._stop_gaps()` | Stop checks, gated by `verify_todos` and `max_stop_blocks` |
| `config.py` `HarnessConfig` | The feature switches listed above |
| `observe/evals.py` `run_eval()`, `mcnemar_exact()` | Ablation harness and paired test |
| `llm/replay.py` `context_fingerprint()` | Hash of what the model saw, to locate replay divergence |

## Related chapters

- [22 Trajectories, replay and evaluation](/en/multi-agent/trajectory-eval) covers analysis at the trajectory level.
- [08 Termination, budgets and loop guards](/en/core/termination-budget) covers the Stop checks and budget limits discussed here.
- [29 Compared with pi](/en/advanced/pi-comparison) contrasts this harness with a deliberately minimal one.
