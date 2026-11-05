# 14 Idempotency and side effects

*Summary. [Read the full chapter in Chinese](/reliability/idempotency).*

## The problem

Retries, crash recovery and "try again after a timeout" all create a second execution. The goal is to make two executions have the same effect as one. Where the external system cannot do that, the harness must say "unknown" instead of guessing.

A caller can never tell a lost request from a lost response, since both look like a timeout. So end-to-end "exactly once" is only ever at-least-once delivery plus idempotent consumption. The harness provides the at-least-once half: after a crash it always takes over and reprocesses unfinished calls. Tools and external systems provide idempotent consumption. When a tool cannot, the harness degrades to at-most-once and reports the unknown, which is safer than repeating a payment.

## How it works

**The key.** `ToolExecutor._ctx()` sets `idempotency_key = f"{run_id}:{call_id}"`. It is stable across restarts because both parts are persisted before any side effect: the run record exists, and the loop writes the assistant message (with its `call_id`s) before handing calls to the executor. Random UUIDs and timestamps differ per attempt, so the receiver cannot recognise a retry. A hash of the arguments merges calls the user wants twice and changes when an approver edits the arguments.

The key identifies one intent, not one set of arguments. If the model decides twice to "notify ops that the deploy finished", those are two intents with two `call_id`s. Merging them needs a business-level key built by the tool, such as `deploy:v1.2.0:notify-ops`. The loop guard is the other defense against such repeats.

This depends on `call_id` being unique within a run. The OpenAI-compatible provider falls back to `new_id("call_")` for a missing id. A fallback based on position in the message (`call_0`) would give the first call of every round the same key.

**Four mechanisms, each with a hole when used alone:**

| Mechanism | What it stops | Where |
|---|---|---|
| Idempotency key | Lets the receiver recognise the same intent | `ToolContext.idempotency_key`, `steps.idem_key` |
| Unique constraint | Applies one key once, even under concurrency | `(run_id, seq)` on events, `(run_id, call_id)` on steps, the notify table's `idem_key` primary key, content-addressed blobs with `INSERT OR IGNORE` |
| Conditional update | Lost updates; also acts as the fencing token | `Store.cas_run()` |
| State-machine guard | Illegal transitions | `Store.resolve_approval()` with `WHERE status='pending'` |

The anti-pattern is check-then-act (`if not exists: send; insert`). The gap between check and write is the duplicate window. Let a unique constraint decide atomically instead.

**Timeout is not "did not happen".** In `ToolExecutor._race()`, a timeout on an external, non-idempotent tool returns status `unknown` with the key and an instruction to verify before retrying. Read-only, local-write and idempotent tools get plain `timeout`, which tells the model to narrow the scope and retry. Bash manages its own timeout and marks `details["timed_out"]`, and `_race()` applies the same rule when the command is external, so a timed-out `git push` is `unknown` while a timed-out `pytest` is an ordinary error. For MCP calls the client sends `notifications/cancelled` when it abandons a request. That is best effort and does not change the status. The harness key is not sent to MCP servers; `idempotentHint=true` is the server's own promise, and the harness relies on it to allow re-execution.

**Write-ahead intent and reconciliation.** The executor writes `step_started` (status `started`, `idem_key`, input hash) before running a tool and finishes the step afterwards. After a crash, a step stuck at `started` is settled as described in [11 Checkpoints and crash recovery](/en/reliability/checkpoint-recovery): `reconcile()` first, then re-execution for read-only or idempotent tools, otherwise `unknown`.

**The demo tool shows the whole contract** (`tools/demo.py`):

- `FakeNotifyService` lives in its own SQLite file, because the harness DB and an external system can never share a transaction, which is the reason reconciliation exists.
- `send()` uses `INSERT OR IGNORE`. The same key with the same content returns the first record. The same key with different content raises `IdempotencyConflict`, because returning the old record would tell the caller its request succeeded when something else was sent.
- `SendNotificationTool` is `EXTERNAL`, `interruptible=False`, `idempotent=True`, and sleeps after the external write, which reproduces the dangerous window: effect done, caller not yet told.
- `reconcile()` calls `lookup(key)`. No record returns `None`. A record with different arguments, typically after an approver edited them, returns an error asking for human review, though the executor still records the step as `reconciled`.

**`reconcile()` is ambiguous.** The base class also returns `None`, so "checked, did not happen" and "cannot reconcile" look the same. The current rule is conservative: after `None`, only read-only or idempotent tools are re-run, and a reliable but non-idempotent tool still gets `unknown`. "Not found" does not prove the request will never arrive, since it may sit in a gateway retry queue. External deduplication is therefore more fundamental than reconciliation.

**Tools that cannot reconcile.** Options, cheapest first: pass the key to the external API and mark the tool `idempotent=True`; add a query endpoint; split prepare from commit; roll back from the shadow git snapshot (local files); escalate to a human task, which is not implemented. Idempotency is judged per tool: `write` overwriting a file is idempotent, `edit` (find and replace) is not.

**Multi-step flows.** The chapter compares Saga (compensations, themselves idempotent, keyed like `<key>:compensate`), TCC (reserve, then confirm) and Outbox (state change and pending event in one local transaction). The harness has no saga engine. The `steps` table is a time-ordered ledger of side effects that one could be built from.

**Edge side.** `EdgeExecutor.on_message()` in `runtime/dispatch.py` keeps an executed table and an in-flight table. A duplicate that arrives mid-execution waits on a shared future, and the handler runs once.

## Key code

| Where | What it does |
|---|---|
| `agent/executor.py` `ToolExecutor._ctx()` | Builds the key `run_id:call_id` |
| `agent/executor.py` `ToolExecutor._race()` | Timeout classification: `timeout` vs `unknown` |
| `tools/base.py` `Tool.reconcile()` | Reconciliation hook; default returns `None` |
| `tools/demo.py` `FakeNotifyService`, `SendNotificationTool` | Reference implementation of dedupe and reconcile |
| `tools/shell.py` `BashTool` | Sets `timed_out` in result details |
| `tools/mcp.py` `MCPClient.request()` | Sends `notifications/cancelled` on abandon |
| `runtime/store.py` `Store.cas_run()` / `resolve_approval()` / `append_events()` | Conditional updates and dedupe by primary key |
| `runtime/dispatch.py` `EdgeExecutor.on_message()` | Executed and in-flight dedupe on the edge |

## Related chapters

- [11 Checkpoints and crash recovery](/en/reliability/checkpoint-recovery) uses the key and the step ledger to settle unfinished calls.
- [12 Cancellation semantics](/en/reliability/cancellation) produces `unknown` results when the grace period expires.
- [13 Retries, circuit breaking and fallback](/en/reliability/retry-fallback) decides which tools may be retried at all.
- [Edge and cloud](/en/advanced/edge-cloud) covers the edge executor in context.
