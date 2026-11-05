# 11 Checkpoints and crash recovery

*Summary. [Read the full chapter in Chinese](/reliability/checkpoint-recovery).*

## The problem

Processes die: OOM kills, deploys, `kill -9`, a lost host. A run that was halfway through a tool call must continue, and an external side effect (an email, a payment) must not happen twice. Two cheap answers fail:

- **Rerun from the original prompt.** The model plans differently the second time, every finished side effect runs again, and all earlier tokens are paid for twice.
- **Replay deterministic code, as Temporal does.** There is no business code between steps to replay. The "computation" of an agent is model calls and tool calls, and their results are already stored as messages.

The harness rebuilds instead: it reassembles the context from storage and continues from the next turn. Only tool calls whose outcome is unknown need special handling.

## How it works

**Pause and crash are different interruptions.** A pause only takes effect at `_safe_point()`, where every tool result of the previous round is stored and paired, so a pause never creates an unknown outcome. A crash can land on any instruction. Graceful shutdown (`Harness.aclose()`) therefore pauses main runs with `paused_by="shutdown"` instead of cancelling them, and `recover()` resumes those after restart. A user pause is left alone until the user resumes.

**Four kinds of stored data, four jobs:**

| Data | Role in recovery |
|---|---|
| `entries` (path from root to head) | The current state. Rebuilds the context |
| `steps` | Write-ahead log: an intent row with no result means the call may have taken effect |
| `events` | Audit stream only. Not used for recovery |
| `runs.checkpoint` | Budget state, a workspace manifest, pause info, queued inbox |

**Detecting a dead worker.** A worker claims a run with a CAS from `queued` to `running` that also writes `worker_id`. A heartbeat task then refreshes `heartbeat_at` with a conditional `UPDATE ... WHERE worker_id IS NULL OR worker_id=?`. Defaults are a 2 second heartbeat and a 10 second staleness threshold. The ratio is deliberately loose, because the heartbeat shares an event loop with tools and a blocking call can delay it.

**Taking over.** `recover()` scans for stale active runs and moves each to `interrupted` with a CAS pinned to the version it saw. Whoever wins the CAS does the follow-up work (expire pending approvals, then `resume()`); a loser gets `TransitionConflict` and skips the run. Child runs are only closed out as `cancelled`, since the parent's `task` call spawns a new child on resume. A run already in `cancelling` is finished as `cancelled`. A second pass handles `queued` orphans and shutdown pauses, which have no heartbeat to check. `recovery_loop()` repeats the scan periodically, because a crashed worker's heartbeat takes time to expire.

**Fencing a worker that is merely stuck.** If a heartbeat finds the lease belongs to someone else, `_fence()` treats the process as crashed: the event hub drops later events and the driver task is cancelled preemptively. Cooperative cancellation would run the normal wrap-up and overwrite the new owner's run.

**Settling unpaired calls.** On resume, `_body()` calls `unpaired_tool_calls()` and runs them with `run_batch(recovering=True)` before it appends the resume note, because a user message between a tool call and its result breaks pairing. Per call:

| Step row | Tool | Action |
|---|---|---|
| none | any | Run normally, including approval |
| `started` | `reconcile()` finds the operation | Use that result (`reconciled`) |
| `started` | not found, read-only or idempotent | Re-run with the same idempotency key |
| `started` | not found, neither | Return an error result, status `unknown`, telling the model to check external state |

The idempotency key is `run_id:call_id`, both unchanged by recovery, which is why lookup works. See [14 Idempotency and side effects](/en/reliability/idempotency).

**The resume note** is a persisted `<system-reminder>` user message, worded by the prior state (failed, paused, interrupted), plus a diff of the workspace manifest if files changed meanwhile.

## What recovery does not restore

Budget is written at each `turn_end`, so usage from a turn cut off mid-way is lost. `LoopGuard` windows, `file_mtimes` and `stop_blocks` live only in memory and reset. The chapter also lists problems left unhandled: expired credentials, dropped long-lived connections, and external state that changed while the run was down. The manifest compares size and mtime for at most 5000 files, so it can report false drift.

Fencing has a stated gap. `append_entry`, `step_started` and `step_finished` do not check the lease, so a stale worker can still write for up to one heartbeat interval. The chapter proposes validating the lease inside each write.

The model is not pinned: continuing an old session on a new model is allowed and recorded as a `model_switched` notice. The system prompt and tool name list are pinned in `sessions.meta`.

## Key code

| Where | What it does |
|---|---|
| `runtime/manager.py` `Harness.recover()` | Stale-run scan, version-pinned CAS takeover, orphan handling |
| `runtime/manager.py` `Harness.recovery_loop()` | Periodic takeover while the server runs |
| `runtime/manager.py` `Harness._heartbeat()` / `_fence()` | Conditional lease renewal and self-fencing |
| `runtime/manager.py` `Harness._checkpoint()` | Writes budget, manifest and pause info to `runs.checkpoint` |
| `runtime/manager.py` `Harness._resume_note()` | Builds the resume message |
| `runtime/manager.py` `Harness._pair_dangling_offline()` | Pairs leftover calls offline; `started` becomes `unknown`, never `cancelled` |
| `runtime/store.py` `Store.heartbeat()` / `stale_runs()` | Conditional renewal and staleness query |
| `agent/executor.py` `ToolExecutor._run_one()` | The `recovering` branch: reconcile, re-execute or mark unknown |
| `runtime/workspace.py` `Workspace.manifest()` / `diff_manifest()` | Drift detection |
| `types.py` `unpaired_tool_calls()` | Finds calls without results |

## Related chapters

- [10 Run lifecycle](/en/reliability/run-lifecycle) defines the states and CAS transitions that takeover relies on.
- [12 Cancellation semantics](/en/reliability/cancellation) covers the `cancelling` path that `recover()` finishes.
- [14 Idempotency and side effects](/en/reliability/idempotency) explains `reconcile()` and the key.
