# 26 Edge-cloud collaboration

*Summary. [Read the full chapter in Chinese](/advanced/edge-cloud).*

## The problem

When orchestration (the "brain") runs in the cloud and execution (the "hands") runs on a desktop, in a browser, on a device or in a remote sandbox, the two sides are separated by a network that drops, duplicates and reorders messages. Either side can also crash. The goal is that each command takes effect exactly once.

The repo implements only the smallest useful slice: command dispatch with idempotent consumption. Handoff of execution, offline sync, resumable artifact upload and a local daemon are design notes in the chapter, not code.

## How it works

The guarantee is built from two halves, at-least-once delivery and idempotent consumption. No single mechanism gives exactly-once. Together they do.

**Cloud side, `CommandDispatcher`.**

- `submit(device_id, payload, idem_key, ttl)` deduplicates on `idem_key`. The same key with the same device and payload returns the existing command. The same key with different content raises `IdempotencyKeyReused` rather than silently dropping the new content.
- Each command has two identities. `idem_key` is the caller's intent and survives a harness restart. `Command.id` is the command's identity on the wire and stays the same across resends.
- The status machine is `pending`, `sent`, then `acked`, `failed` or `expired`. Every move goes through `_move()`, an in-memory conditional update. A second ACK, a late ACK or an ACK for an unknown id moves nothing, so `on_ack()` returns `False`.
- `pump()` has no timer. The caller invokes it. It checks TTL first, then whether the device is online, then whether a resend is due. An offline device keeps its commands queued, and a command that expired while offline is never sent. A command still `sent` after `max_attempts` becomes `failed`.

**Edge side, `EdgeExecutor`.** Two tables absorb duplicates. `executed` maps command id to its ACK and handles copies that arrive after execution: the executor resends the earlier ACK, because the cloud probably resent the command when that ACK was lost. `_inflight` holds a future registered before the handler starts and handles copies that arrive mid-execution: they wait for the same result instead of running the handler again. Both tables are in memory, so an edge process that crashes mid-handler loses them.

**Terminal states do not mean "not executed".** `acked` is confirmed. `failed` after `max_attempts` is unknown, since every attempt may have run with only the ACK lost. `expired` from `sent` is unknown, and the edge does not check TTL, so a delayed copy can still run. Only `expired` from `pending` means the command never left. The chapter says the upper layer should send unknown outcomes to reconciliation, not tell the model to retry.

**Checklists for production.** The chapter poses six questions (source of truth, idempotency key, offline capability, reconnect reconciliation, who can take over, artifact delivery) and four timing questions for every cross-system event: what if it is late, duplicated, reordered or never arrives. It marks which answers the repo has and which are gaps. The gaps include persisting the command table, a reconnect handshake, per-device sequence numbers and a persisted edge dedup table.

**Handoff, as design.** The four steps are fencing, quiescing to a safe point, migrating state and resuming with a note to the model. The first, second and fourth map onto existing run version CAS, `Harness.pause()` and `Harness._resume_note()`. On migration, credentials are reissued rather than moved, the remaining budget travels with the run, and pending approvals are reissued in the new environment.

## Key code

| Where | What it does |
|---|---|
| `runtime/dispatch.py` `CommandDispatcher.submit()` | Idempotent submit; raises `IdempotencyKeyReused` on a conflicting reuse |
| `runtime/dispatch.py` `CommandDispatcher._move()` | Conditional state move; terminal states are immutable |
| `runtime/dispatch.py` `CommandDispatcher.pump()` | TTL check, offline queueing, timed resend, `max_attempts` |
| `runtime/dispatch.py` `CommandDispatcher.on_ack()` | Accepts the first ACK only; ignores duplicates and late ones |
| `runtime/dispatch.py` `EdgeExecutor` | `executed` and `_inflight` tables for idempotent consumption |
| `runtime/dispatch.py` `FlakyTransport` | Test transport with drop, duplicate and delay |
| `runtime/store.py` `BlobStore` | Content-addressed storage, the model for artifact delivery |
| `examples/09_edge_cloud.py` | Runs ten commands over a lossy network; each executes once |

## Related chapters

- [14 Idempotency and side effects](/en/reliability/idempotency) defines the `run_id:call_id` key used here.
- [11 Checkpoints and crash recovery](/en/reliability/checkpoint-recovery) covers worker takeover by heartbeat and CAS.
- [27 Protocols: MCP, A2A and Skills](/en/advanced/protocols) covers the MCP side of a local daemon.
