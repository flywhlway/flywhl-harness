# 10 The run state machine

*Summary. [Read the full chapter in Chinese](/reliability/run-lifecycle).*

## The problem

Many parties touch a run at once: the background loop, a stop button, an approval dialog, a second worker taking over after a missed heartbeat, and late callbacks. If the status is a few booleans (`is_running`, `is_cancelled`, `waiting_approval`), most of the 16 combinations are meaningless. Worse, each writer updates only its own flag. A user cancels, then a late approval callback sets `is_running=True`, and the run comes back to life.

The harness needs one status field, a whitelist of legal moves, and a rule that exactly one concurrent writer wins.

## How it works

**Fourteen states in four groups.** Initial: `created`, `queued`. Active (`ACTIVE`): `running`, `waiting_human`, `cancelling`. These need a heartbeat, and a stale one marks the worker as crashed. Suspended: `paused`, `interrupted`. No worker runs them and they wait for `resume()`. Terminal: `completed`, `cancelled`, `failed`, `timed_out`, `budget_exhausted`, `loop_detected`, `incomplete`. `paused` is outside `ACTIVE`, so a run paused for a week is never mistaken for a crash.

**Transitions are a table.** `TRANSITIONS` maps each state to the states it may enter, and anything else raises `IllegalTransition`. Terminal states have no exits, with one exception: `failed` can go to `queued` through `retry()`, which adds 1 to `attempt`. Three edges in the table have no caller yet (`waiting_human` to `cancelled`, `cancelling` to `interrupted`, `interrupted` to `failed`).

**Similar states stay separate.** `paused` is a planned leave. `interrupted` means the worker died, possibly mid side effect. `waiting_human` holds a live coroutine on an approval. `incomplete` is a finished run whose acceptance checks never passed. Merging them into `blocked` would lose the facts that decide who may advance the run, what timeout applies, and what to tell the model on resume. `_resume_note()` writes a different note for paused, interrupted and failed runs.

**CAS with two checks.** `RunStateMachine.transition()` validates the edge against `TRANSITIONS`, then applies `cas_run` on `(status, version)`. `expect` states the caller's precondition, such as "only if still queued". On a CAS failure it re-reads and re-judges instead of retrying blindly. If the new state still allows the move, it retries (up to 3 times). If not, it raises `IllegalTransition`, which tells the caller it lost the race. That is how `cancel()` returns `False` when the run has just completed.

**`expect_version`.** A decision based on an observation, such as crash takeover, pins the version it saw and tries once. If another worker already took over and the run is `running` again, a plain re-read would see a legal move and steal it. With the pinned version the move fails with `TransitionConflict`. `recover()` uses it for every takeover.

**Cancel versus finish.** `cancel()` first moves the run to `cancelling` by CAS, then signals the token. `_finish()` reads the current state, and if it is `cancelling` it ends as `cancelled` even when the loop computed `completed`, noting the loop result in `status_reason`. The state must match the reply the user already got. The reverse order loses cleanly: the run completes first and `cancel()` returns `False`.

**Approval and cancel arriving together.** `CancelToken.guard()` prefers a ready result over a cancel signal, so the broker can still return "approved". The guard is a last `raise_if_cancelled()` in `ToolExecutor._run_one` before the intent row is written. The call then gets a `skipped` result and the side effect never goes out. A stored approval of `approved` therefore means someone approved it, not that it executed. The `steps` table says whether it ran.

**Late writes.** `_set_status` swallows `IllegalTransition`, so a late "back to running" after `cancelling` is rejected by the machine and the writer needs no knowledge of the race. `_drive()` claims the lease with a `queued` to `running` CAS and exits quietly if it loses, so a lost claim is never recorded as `failed`.

**Timeouts and audit.** Approval waits time out (600 seconds by default) and resolve as rejected, itself via CAS so a human decision that lands at the same moment wins. That timer lives in the process, so a dead process loses it. Every transition writes a `status` event with from, to, reason and version. Transitions without a live event hub use `_offline_transition()` and `append_event_next`. The CAS and the event are separate commits, so a crash between them can lose an audit line but not the state.

## Key code

| Location | What it does |
|---|---|
| `runtime/state.py` `RunStatus`, `TRANSITIONS`, `ACTIVE`, `TERMINAL` | States, legal edges, groupings |
| `runtime/state.py` `RunStateMachine.transition()` | Edge check, `expect`, `expect_version`, CAS with re-judging |
| `runtime/manager.py` `Harness.cancel()`, `_finish()` | Claim `cancelling` first, resolve the final state |
| `runtime/manager.py` `_drive()`, `_set_status()` | Lease-claiming CAS, rejection of late writes |
| `runtime/manager.py` `_transition()`, `_offline_transition()` | Transitions that always write a `status` event |
| `runtime/manager.py` `recover()`, `resume()`, `retry()` | Takeover and re-queuing |
| `agent/executor.py` `ToolExecutor._run_one()` | Last cancel check before a side effect |
| `policy/approvals.py` `ApprovalBroker.request()` | Approval timeout and decide-once CAS |

## Related chapters

- [09 State and persistence](/en/reliability/state-persistence) defines `cas_run` and `version`.
- [11 Checkpoints and crash recovery](/en/reliability/checkpoint-recovery) covers leases, heartbeats and what `interrupted` leads to.
- [12 Cancellation](/en/reliability/cancellation) covers the `cancelling` phase in detail.
- [08 Termination and budgets](/en/core/termination-budget) explains how the terminal states are chosen.
- [17 Permissions and human approval](/en/governance/permissions-hitl) covers the approval side.
