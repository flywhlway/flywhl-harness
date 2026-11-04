# 12 Cancellation semantics

*Summary. [Read the full chapter in Chinese](/reliability/cancellation).*

## The problem

"Stop" looks like a boolean, but it has to answer several questions at once. What happens to half-written model output? To a running `npm test`? To a notification already sent whose reply hasn't arrived? To subagents? And when cancel and completion race, which wins?

asyncio's `task.cancel()` is preemptive: it raises `CancelledError` at any `await`. That can interrupt a request that has already left the process, leaving an outcome nobody knows. The harness wants a cancel request, not a cancel command. A token travels down through the loop, the model stream, the tools and the subagents, and each layer stops at its own safe point.

## How it works

**`CancelToken`** (`cancel.py`) is built on an `asyncio.Event`:

- `cancel(reason, by)` is idempotent and records only the first reason and initiator.
- Cancellation cascades down to child tokens, never up. A subagent that fails does not cancel its parent.
- A child created from an already-cancelled parent starts cancelled. This closes a race where a parent is cancelled just as it spawns a subagent.
- `guard(aw)` races any awaitable against the cancel event. If the event wins, it cancels the awaitable, waits for its cleanup, then raises `RunCancelled`. If both are ready at once, the result wins, so callers must not assume cancel did not happen after `guard` returns.
- `iterate(stream)` wraps each `__anext__` in `guard` and calls `aclose()` in `finally`, so the HTTP connection is released and the server stops generating.

**What each layer does on cancel:**

| Layer | Behavior |
|---|---|
| Model stream | Text already generated is kept as an assistant message with `stop_reason=aborted`, because the user saw it. A half-built tool call is dropped |
| Read-only or local tool | Aborted at once. Bash gets SIGTERM to the whole process group, then SIGKILL after 2 seconds |
| Irreversible external tool (`git push`, `curl -X POST`, non-read-only MCP) | Not interrupted. It gets a grace period (`cancel_grace` on the tool, else `cancel_grace_seconds`, default 5) and is marked `completed_after_cancel` if it finishes, or `unknown` with its idempotency key if not |
| `task` (subagent) | Not interruptible, 30 second grace. The child token cancels the child loop, which writes its own terminal state |
| Approval wait | `guard` raises, the approval is expired, the call gets an "unexecuted" result |
| Retry backoff | `cancel.sleep()` is interrupted |

Interruptibility is decided per call by `Tool.interruptible_for(args)`. For bash it follows the command analysis, so `ls` can be killed and `git push` cannot. `write` and `edit` contain no `await`, so they finish once started, and an atomic write (temp file, fsync, `os.replace`) covers a process crash.

**One last check before a side effect.** After approval and hooks, `_run_one()` calls `raise_if_cancelled()` before writing the step intent. Without it, a cancel and an approval that both arrive before the executor wakes would let the approval win and start a new side effect.

**Pairing repair.** Every `tool_use` needs a result or the next request gets a 400. Synthetic results are written in three places: `run_batch` for calls never started (`skipped`), `_pair_dangling()` in the loop (`cancelled`), and `_pair_dangling_offline()` for offline cancels. A call whose step is `started` is recorded as `unknown`, never `cancelled`. Under preemptive termination `run_batch` writes nothing, so those calls stay `started` for recovery.

**State.** `Harness.cancel()` does a CAS from `running` or `waiting_human` to `cancelling`, then fires the token. The intermediate state exists because cancelling takes time, because the CAS decides the race with completion in the database, and because a crash during cancel leaves a `cancelling` run that recovery finishes instead of resuming. The initiator (`web-user`, `terminal-user`, `hook`, `system:fencing`) is carried into `status_reason`.

**Three cases that are not a plain cancel:**

- **Shutdown** is a pause, not a cancel: the user's intent is "continue later". `aclose()` sets `paused_by="shutdown"`, and runs that do not reach a safe point within the grace period are cancelled preemptively and later treated as crashes.
- **Lost lease** uses preemptive cancel on purpose. `_fence()` stops the driver task and drops events, because a cooperative wrap-up would overwrite a run that now belongs to another worker.
- **Subagents.** If the parent is cancelled, `run_subagent` re-raises `RunCancelled`. If only the child is cancelled, the parent receives a `status=failed` return contract and decides what to do. A preemptively terminated parent still drives the child to a terminal state.

**CLI.** The first Ctrl-C cancels cooperatively. A second within 1.5 seconds raises `SystemExit(130)`, a preemptive stop that leaves the run in `cancelling` for `flywhl recover` to settle.

## Limits worth knowing

- Synchronous blocking code never sees the token. Threads started with `asyncio.to_thread()` cannot be cancelled either.
- Swallowing `CancelledError` with `except BaseException` leaves a task that cannot stop.
- There is no automatic compensation. The chapter lists what a saga needs (idempotent compensations, intent written first, reverse order, human fallback) and says the harness provides only the prerequisites: step intents, deterministic keys, honest `unknown` results and workspace snapshots.

## Key code

| Where | What it does |
|---|---|
| `cancel.py` `CancelToken` (`cancel`, `child`, `guard`, `iterate`, `sleep`) | The token and its racing primitives |
| `agent/loop.py` `_safe_point()` / `_call_model()` / `_pair_dangling()` | Loop-level cancel, partial text handling, pairing |
| `agent/executor.py` `ToolExecutor._race()` | Tool vs cancel race, grace period, `unknown` result |
| `tools/base.py` `Tool.interruptible_for()` | Per-call interruptibility |
| `tools/shell.py` `_terminate()` | SIGTERM, then SIGKILL, on the process group |
| `agent/subagent.py` `TaskTool` | Uninterruptible, 30 second grace |
| `runtime/manager.py` `Harness.cancel()` / `aclose()` / `_fence()` / `run_subagent()` | State transition, shutdown pause, self-fencing, cascade |
| `cli.py` `_stream_run()` | Two-step Ctrl-C |

## Related chapters

- [11 Checkpoints and crash recovery](/en/reliability/checkpoint-recovery) handles runs left in `cancelling` and calls left `started`.
- [14 Idempotency and side effects](/en/reliability/idempotency) explains the key attached to `unknown` results.
- [04 The agent loop](/en/core/agent-loop) describes the safe points where cancel is raised.
