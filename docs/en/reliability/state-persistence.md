# 09 State and persistence

*Summary. [Read the full chapter in Chinese](/reliability/state-persistence).*

## The problem

A run lasts minutes to hours. The process can die, the browser can disconnect, and the user can go back and branch from an earlier step. To resume after `kill -9`, the harness has to know what it must store, at what granularity, and where each atomic write boundary sits.

Putting everything in one table fails because the pieces have different write rates and lifetimes. A message is written once and never changed. A run's status is updated in place and raced over by workers. A tool call's journal row must answer "did this side effect happen?", which neither of those can.

## How it works

State is split by the question it answers, in one SQLite file (WAL mode, `durability = "full"` by default) with seven tables:

| Table | Answers | Mutability |
|---|---|---|
| `entries` | What the model sees next | Append-only tree |
| `sessions` | Which branch is current, plus pinned prompt and tools | `head` and `meta` updated in place |
| `runs` | Phase, owner, spend, checkpoint | In-place, changed by CAS only |
| `steps` | Did this side effect happen? | Written twice per call |
| `events` | Where a disconnected UI resumes | Append-only, may be purged |
| `approvals` | Who approved what | Decided once |
| `blobs` | Where large outputs live | Immutable, content-addressed |

The granularity runs Session, Run, Step, Event. Unlike a generic workflow engine, the harness needs no deterministic replay. The two big nondeterministic results, model output and tool output, are already context content once stored in `entries`. Recovery rebuilds the context and runs the next round. Events are an audit and playback stream and do not take part in recovery.

**Sessions.** Appending an entry and moving `head` happen in one transaction. A branch only moves `head`; old branches stay. `Store.path()` walks from `head` to the root with a recursive query, which costs O(path length) per call. The entry id is `e_<message id>` under a UNIQUE constraint, so a message cannot be stored twice. Session `meta` pins the rendered system prompt and the tool list at creation, which keeps the cached prefix stable and makes a resume use the same instructions even if the code was upgraded. Branching does not roll back workspace files.

**Runs.** `cas_run` is a single `UPDATE ... WHERE status=? AND version=?`. Zero rows changed means someone else moved first. `version` grows on every transition, so a state that cycles back is not mistaken for the old one. `update_run` asserts that `status` is never in its fields.

**Steps.** `step_started` writes the intent before the side effect, in its own commit. `step_finished` writes the end state. Recovery looks for rows that are `started` with no end state. The key is `run_id:call_id` and is fixed across retries. Calls rejected before execution never get a row.

**Atomic boundaries.** Entry plus head, tool result plus step end state, and each event batch are one transaction each. `Store.tx()` is reentrant, with `BEGIN IMMEDIATE` on the outermost call so lock conflicts become waiting, not `SQLITE_BUSY`. Crash analysis follows from this: a crash before the intent row means the call never ran; a crash after it means the outcome is unknown and needs reconciliation.

**Events never lead persistence.** `AgentLoop._append` stores the entry, then emits `message_end`. The reverse order could show the user a statement that recovery cannot find. Streaming deltas (`message_update`, `tool_update`) are buffered and flushed in batches of 64 or after 50 ms, inside one transaction per batch, because autocommit would otherwise cost one WAL commit per row.

**Durability limit.** Both settings survive a process crash. The default `durability = "full"` sets `synchronous=FULL`, `fullfsync` and `checkpoint_fullfsync`, so a committed transaction also survives power loss, provided the disk honours the flush. `fullfsync` matters on macOS, where a plain `fsync` leaves data in the drive's write cache; it has no effect elsewhere. `durability = "normal"` (`synchronous=NORMAL`) may roll back the last few transactions after power loss, possibly including an intent row. Recovery then sees no record and a non-idempotent call runs again. On Linux the database stays consistent; on macOS checkpoints use a plain `fsync` too, so consistency depends on the disk not reordering writes. It relaxes the guarantee, so a project config needs trust before it takes effect.

The guarantee covers the session database (`harness.db`) only. Shadow git snapshots and workspace files are outside it: atomic replace prevents half-written files, but the directory is not fsynced, so a file can revert to its old content after power loss. Nor does it close the gap between sending a side effect and recording the result. A deterministic idempotency key is still the last line of defence.

The cost lands on commit latency. `Store` writes synchronously on the event loop thread, so a slow commit stalls SSE and approvals. On one Apple M5 Pro internal SSD (APFS), `F_FULLFSYNC` takes about 4 ms, so a `full` commit has a p50 near 4 ms against about 0.05 ms for `normal`. One session streaming at a fast model's pace is 15% slower per turn. Four concurrent sessions take about twice as long per turn, and the event loop stalled for close to a second at worst. These numbers describe that machine and disk only, and slow disks were not measured. See the [measurements](https://github.com/flywhlway/flywhl-harness/blob/main/docs/quality-audit/evidence/sqlite-durability-2026-11-06.md).

## Key code

| Location | What it does |
|---|---|
| `runtime/store.py` `SCHEMA`, `Store.tx()` | Seven tables and the reentrant transaction |
| `runtime/store.py` `append_entry()`, `path()` | Append plus head move, recursive path query |
| `runtime/store.py` `cas_run()`, `update_run()` | Optimistic state transitions |
| `runtime/store.py` `step_started()`, `step_finished()` | Intent and result journal |
| `runtime/store.py` `append_event_next()`, `append_events()` | Offline events and batched writes |
| `runtime/store.py` `resolve_approval()`, `BlobStore` | Decide-once approvals, scoped blobs |
| `runtime/session.py` `SessionLog.branch()`, `context()` | Branching and the model's view |
| `runtime/manager.py` `Harness.create_session()`, `EventHub` | Pinned meta, event buffering and fan-out |
| `tools/fs.py` `_atomic_write()` | Workspace files written via temp file and `os.replace` |

## Related chapters

- [10 The run state machine](/en/reliability/run-lifecycle) builds on `cas_run` and `version`.
- [11 Checkpoints and crash recovery](/en/reliability/checkpoint-recovery) reads the `steps` journal to decide what to redo.
- [14 Idempotency](/en/reliability/idempotency) explains why the key is `run_id:call_id`.
- [15 Streaming and steering](/en/reliability/streaming) covers resuming from `events` by `seq`.
- [07 Context engineering](/en/core/context-engineering) covers how compaction entries rewrite the view.
