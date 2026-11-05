# 04 The agent loop

*Summary. [Read the full chapter in Chinese](/core/agent-loop).*

## The problem

"Call the model, run the tools, call the model again" fits in a ten-line `while` loop, and that loop breaks on contact with reality:

| What happens | Ten-line loop | What the harness does |
|---|---|---|
| The user clicks stop | `task.cancel()` can leave a tool call with no result, and the next request gets a 400 | Cooperative cancellation, raised only at safe points |
| The process is killed | `messages` was in memory and is gone | Every message is persisted before its event is sent |
| Output is cut off by `max_tokens` | A `write` runs with half its arguments | The whole batch is marked failed and nothing runs |
| The model says it's done | The loop ends, whether or not the artifact exists | Stop checks: the plan, a verify command, Stop hooks |
| An unexpected exception | The run stays `running` forever | The outer wrapper ends it as `failed` and keeps the evidence |

The answer is to cut each round into phases with clear boundaries, so that no interruption can land between a tool call and its result.

## How it works

Each round of `AgentLoop._body()` runs in a fixed order, and the order is the design:

1. **Safe point.** The top of the loop is the one moment when the last batch of tools all have results and the next model call hasn't been sent. Cancel and pause take effect here.
2. **Steering.** A steer becomes a user message here, for the same reason: it can't land between a call and its result.
3. **Budget check.** This comes before compaction, so an exhausted run doesn't spend another model call compacting. A hard limit calls `_wrap_up()`, which asks for a final summary with `tool_choice=none`.
4. **Compaction check.** Runs on estimated context usage. On a real overflow error the loop compacts by force and retries once.
5. **Reminders.** Budget warnings, loop warnings and plan recitation are merged into one persisted user message.
6. **Model call.** Streaming, with retries and fallback handled in the provider layer. The loop only sees `retry` events.
7. **Persist the assistant message.** This is the checkpoint anchor. Whatever crashes later, recovery can find the unfinished calls from this message.
8. **Branch.** With tool calls, `executor.run_batch()` runs them and the loop guard observes the results. With none, queued input goes first, then `_stop_gaps()` decides whether the run has really finished.

`max_turns` counts model calls, not conversation rounds: a turn here is one `_call_model()` plus the tools after it, the same definition as pi. Codex's "turn" is closer to a whole run.

The loop is an async generator because it can then yield events at the point they happen, stay independent of the UI, and hand control back at every step. The outer `run()` guarantees an explicit end state whatever `_body()` does: any exception ends the run as `failed` with the traceback kept. When a run is cancelled with tool calls unanswered, `_pair_dangling()` writes synthetic results, so the pairing invariant holds for the next request.

## Key code

| Where | What it does |
|---|---|
| `agent/loop.py` `AgentLoop.run()` | The outer wrapper: always ends with an explicit status |
| `agent/loop.py` `AgentLoop._body()` | The main loop and its phase order |
| `agent/loop.py` `_safe_point()` | Raises cancel or pause at the loop boundary |
| `agent/loop.py` `_call_model()` | Streaming model call; the source of `_ContextOverflow` |
| `agent/loop.py` `_stop_gaps()` | Stop checks before a run may end |
| `agent/loop.py` `_pair_dangling()` | Writes synthetic results for tool calls left unanswered by a cancel |
| `agent/inbox.py` `Inbox` | The steer and follow-up queues |

## Related chapters

- [07 Context engineering](/en/core/context-engineering) and [08 Termination and budgets](/en/core/termination-budget) fill the compaction and budget slots.
- [12 Cancellation](/en/reliability/cancellation) covers what happens at a safe point.
- [15 Streaming and steering](/en/reliability/streaming) covers how input reaches the inbox.
