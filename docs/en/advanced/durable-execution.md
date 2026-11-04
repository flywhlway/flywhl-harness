# 25 Mapping to durable execution engines

*Summary. [Read the full chapter in Chinese](/advanced/durable-execution).*

## The problem

Chapters 09 to 14 build durability by hand on SQLite: append-only entries, a write-ahead journal for tool calls, a CAS state machine and heartbeat leases. This chapter compares that design with Temporal, the industrial engine for the same job. It asks how the agent loop would be split inside Temporal, what constraints it would hit, and when migrating is worth it. Only the flywhl side is implemented. The Temporal side is a mapping, and the workflow code in the chapter is a sketch with assumed activity names.

## How it works

**Two ways to recover.** Durable execution rests on recording every uncertain result (network replies, time, randomness, human decisions) and rebuilding state from the record. Temporal replays the orchestration code against the event history, so that code must be deterministic. flywhl rebuilds state instead. `AgentLoop._body` does not replay past turns. It rebuilds the context from `entries`, reconciles tool calls that lack results against `steps` (`unpaired_tool_calls()` then `run_batch(recovering=True)`), adds a resume note and goes on to the next turn. This works because the loop keeps almost no memory state between turns: the message history is its state.

The "almost" is where the two approaches differ most. `Harness._checkpoint` writes `Budget.state()` each turn, and `Budget.restore` continues the count. Taint and grants live in `session.meta`. The `LoopGuard` window and the `stop_blocks` count are not persisted and restart from zero. An engine that replays code never has to decide which variables to store.

**Where the loop would be cut in Temporal.** The turn loop, termination, budget counting and the "continue or stop" decision are pure computation and go in the workflow. Model calls, each tool execution, compaction summaries, the verify command and out-of-process hooks are activities. Waiting for approval or a steer becomes a signal plus `wait_condition`, which holds no worker while waiting.

**What would need rewriting for determinism:**

| In flywhl | In a workflow |
|---|---|
| `new_id("m_")` for message IDs | `workflow.uuid4()` |
| `now()` timestamps and the `time.monotonic` wall-clock budget | `workflow.now()` or a durable timer |
| Reading context from SQLite in `_call_model` and `_maybe_compact` | Move into an activity |
| Tool-timeout race in `ToolExecutor._race` | Activity `start_to_close_timeout` |
| Backoff in `ToolExecutor._invoke` | Activity retry policy or `workflow.sleep` |

**Approval.** The approval record is durable (the `approvals` table, resolved by CAS on `status='pending'`), but the waiting is in memory. `ApprovalBroker` waits on a future for `approval_timeout` (600 seconds by default, and timeout means deny). A crash loses the wait. A Temporal wait is an open condition and timer in the history, so waiting three days costs what waiting three seconds costs. This is the most basic difference for long human review.

**History size.** Temporal limits history and payload size, so passing the message list through activities breaks. The answer is claim check plus continue-as-new: keep only a reference to the context head and the counters in workflow state, and restart the history periodically. flywhl already stores context in `entries`, large results in `BlobStore`, and the head as a pointer, so its storage layer could serve as the claim-check store unchanged. Activities run at least once, so writes must be idempotent. Message entries use `entry_id=f"e_{msg.id}"` with a `UNIQUE` constraint, and the ID is generated in the workflow so it survives replay.

**Streaming.** An activity cannot stream tokens to its workflow. Deltas have to go through a side channel such as a message bus to an SSE gateway, while only the final message reference enters the history. `EventHub` with a monotonic `seq` and `Last-Event-ID` resume is the single-process version of that pattern.

**Versioning.** Changing a workflow's branches breaks replay of old histories, which Temporal handles with `patched`. flywhl has no replay, so it pins inputs instead: `create_session` freezes the system prompt, model and tool names in `session.meta`. Switching models is allowed but leaves a `model_switched` notice and updates `meta`.

**Idempotency stays your job.** Temporal gives the workflow exactly-once semantics, but activities are at least once. The idempotency key is `run_id:call_id` here, the equivalent of `workflow_id:call_id` there. A non-idempotent tool gets one attempt, and a timeout means unknown outcome, resolved by a reconcile step (`Tool.reconcile`).

**Side by side.**

| Dimension | flywhl-harness | Temporal |
|---|---|---|
| Source of truth | `entries`, `steps`, `events`, `runs` | One event history |
| Recovery | Rebuild state and continue | Replay code, reuse recorded activity results |
| Long waits | In memory, expire on crash | Signals and durable timers, days |
| Lost worker | Heartbeat lease (2 s heartbeat, 10 s stale), CAS takeover | Server detects heartbeat timeout |
| Concurrent takeover | CAS pinned to the version seen at scan, so one worker wins | Server guarantees one execution advances |
| History size | No hard limit, controlled by compaction and blobs | Hard limits, needs claim check and continue-as-new |
| Operations | One SQLite file | A cluster or managed service, worker versioning |

Takeover safety has three layers. `recover()` pins the `running` to `interrupted` transition to the version it scanned (`expect_version`, no retry on conflict). `_drive` claims with `expect={QUEUED}` and exits quietly if someone else won. `Store.heartbeat` renews only while `worker_id` is still the caller's, and `_fence()` treats a failed renewal as a crash.

**Choosing.** Temporal restores by replay and constrains your code strongly. A queue plus state table (flywhl) restores by rebuilding and constrains little but leaves leases, takeover and wake-ups to you. LangGraph's checkpointer saves graph state after each node, which suits state management and interrupt-and-resume for one reasoning graph. The two layers also combine: Temporal as the outer workflow across hours, the harness as the inner loop inside an activity. Temporal cannot see inside the activity, so an activity retry must resume the same run, not start a new one.

Migrate when waits last hours or days, deployments span machines with frequent rolling restarts, orchestration includes non-agent steps, audits demand reproducible state at any moment, or you find yourself writing durable timers and distributed locks. Stay on the hand-built design for single-machine, minutes-long runs where the session tree should be the storage model. The `Tool` interface and `reconcile`, the `Provider` abstraction, `entries` and `BlobStore`, and the event model with `seq` carry over. The loop control flow, `Budget` timing and `ApprovalBroker` waiting would be rewritten.

## Key code

| Where | What it does |
|---|---|
| `agent/loop.py` `AgentLoop._body()` | Resumes by reconciling unanswered tool calls, not by replaying turns |
| `types.py` `unpaired_tool_calls()` | Finds tool calls with no result |
| `agent/executor.py` `run_batch(recovering=True)` | Reconciles or marks `unknown` for calls left from a crash |
| `runtime/manager.py` `_checkpoint()`, `agent/budget.py` `Budget.restore()` | Persist and continue the budget counters |
| `runtime/manager.py` `recover()`, `recovery_loop()` | Scan stale runs and take them over |
| `runtime/manager.py` `_fence()` | Self-fence when the lease moved |
| `runtime/state.py` `transition(expect_version=...)` | Conditional state change with no retry on conflict |
| `runtime/store.py` `heartbeat()`, `stale_runs()` | Lease renewal and stale detection |
| `policy/approvals.py` `ApprovalBroker` | In-memory approval wait over durable records |
| `runtime/store.py` `BlobStore` | External large results, usable as a claim-check store |

## Related chapters

- [11 Checkpoints and crash recovery](/en/reliability/checkpoint-recovery) and [09 State persistence](/en/reliability/state-persistence) are the design being compared.
- [10 The run state machine](/en/reliability/run-lifecycle) covers the CAS state machine and leases.
- [14 Idempotency](/en/reliability/idempotency) covers keys and reconciliation.
- [15 Streaming and steering](/en/reliability/streaming) covers `seq` and resume.
- [24 Long-running harnesses](/en/advanced/long-running) covers handoff between sessions.
