# 07 Context engineering and compaction

*Summary. [Read the full chapter in Chinese](/core/context-engineering).*

## The problem

A larger window does not fix long tasks. Recall of specific details drops as input grows, and information in the middle of the context is used worst. In a long run, the original goal drifts toward that middle. The agent also resends the full history every turn, so prefix caching lowers the price but not the attention problem.

Every fix conflicts with something else. Rewriting old messages breaks the prefix cache. Rewriting history can invalidate Claude's signed thinking blocks. A summary that runs at the wrong moment can split a tool call from its result and make the next request invalid.

## How it works

The harness orders its tools from cheapest to most lossy and uses the next one only when the previous is not enough:

1. Oversized tool results become blob handles on arrival (see chapter 06).
2. Todo and files carry state outside the window.
3. Plan recitation: every 4 assistant turns, if unfinished todos exist, the plan is appended again as a `<system-reminder>`. It is a persisted message, so history stays append-only and replay shows what the model saw.
4. Old-result clearing: past `clear_threshold` (0.55 of the window), large old tool results and long tool arguments are replaced in the request by a placeholder that includes the handle. History is not touched. The cut moves on a forward-only watermark that jumps once, to before the last `keep_tool_results_turns` (3) turns, then stays put. The request only grows at the tail until the next jump, so the cached prefix survives. Clearing is off for models marked `append_only`, which includes the Claude entries, so they rely on compaction alone.
5. Compaction: past `compact_at`, earlier history becomes a structured summary.

**Level estimate.** `ContextManager.estimate()` takes the last assistant message's real `usage` plus an estimate for later messages. It ignores any usage from before the latest compaction entry, because those messages were generated against the old, larger context. Trusting them would trigger a second compaction at once and produce a summary of a summary. `compact_at` is the smaller of `window * 0.80` and `window - 8192`, so small windows keep room for output.

**Compaction rules.**
- It runs only at the top of a round, when every tool call has its result.
- `find_cut_index` never cuts on a tool result. It prefers a real user message, then an assistant message. The kept segment can be shorter than asked.
- The kept amount is `min(keep_recent_tokens, window * 0.3)`, and 2000 when forced, so a small window does not exceed the threshold again straight away.
- A second compaction updates the previous summary instead of re-summarizing it.
- The original task text and the lists of read and modified files are kept by code, not by the model, because a model paraphrase can drop a constraint such as "do not rename response fields".
- If the summary call fails, `deterministic_summary` takes over so the run continues. `payload["fallback"]` records it.
- Compaction appends an entry. Nothing is deleted. `SessionLog.context()` builds the view as the summary, the kept segment without thinking blocks, then new messages.

**Overflow.** A provider context-overflow error cannot be retried unchanged. The loop forces a compaction and retries the round once. The counter resets after every successful model call, so the limit is once per round, not once per run.

**Reminders** are also escaped. `shape_tool_result` neutralizes any `<system-reminder>` tag found in tool output, so fetched content cannot pose as the harness.

## Key code

| Location | What it does |
|---|---|
| `agent/context.py` `ContextManager.estimate()`, `thresholds()` | Level from real usage, and the two thresholds |
| `agent/context.py` `apply_clearing()` | Placeholder substitution with the forward-only watermark |
| `agent/context.py` `todo_reminder()` | Plan recitation |
| `agent/compaction.py` `find_cut_index()` | Cut point that avoids tool results |
| `agent/compaction.py` `Compactor.compact()` | Summary generation, incremental update, fallback |
| `llm/mock.py` `deterministic_summary()` | Model-free fallback summary |
| `agent/loop.py` `_maybe_compact()`, `_inject_reminders()` | When compaction and reminders run |
| `runtime/session.py` `SessionLog.context()` | Builds the view from the compaction entry |

## Related chapters

- [06 The tool system](/en/core/tools) produces the handles that clearing points back to.
- [04 The agent loop](/en/core/agent-loop) shows where these checks sit in each round.
- [08 Termination and budgets](/en/core/termination-budget) uses the todo list for the stop check.
- [20 Subagents](/en/multi-agent/subagents) isolate noisy exploration in a fresh context.
- [22 Trajectory and evaluation](/en/multi-agent/trajectory-eval) covers probe questions for measuring what a summary lost.
