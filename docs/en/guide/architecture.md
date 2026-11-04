# 03 Architecture overview

This page does not go deep on any one mechanism. It walks one run from input to end and names the file and function where each step happens. Later chapters zoom into one block of this map.

## Module map

```mermaid
flowchart TB
    subgraph Clients["Event consumers"]
      CLI[cli.py<br/>terminal renderer]
      WEB[server/app.py + web/<br/>REST + SSE + UI]
      SDK[Your code<br/>Harness.run / subscribe]
    end
    subgraph Runtime["runtime/"]
      H[manager.py<br/>Harness, EventHub, recovery, subagents]
      SM[state.py<br/>Run state machine, CAS]
      ST[(store.py<br/>SQLite: sessions/entries/runs<br/>steps/events/approvals/blobs)]
      SL[session.py<br/>session tree, context view]
      WS[workspace.py<br/>path confinement, drift, shadow git]
    end
    subgraph Agent["agent/ (the brain)"]
      LOOP[loop.py<br/>AgentLoop]
      EX[executor.py<br/>tool execution pipeline]
      CTX[context.py + compaction.py<br/>usage, clearing, compaction]
      BG[budget.py<br/>budget, loop guard]
      SUB[subagent.py<br/>task tool, contract]
    end
    subgraph Policy["policy/"]
      PERM[permissions.py + bash_analyzer.py]
      HOOK[hooks.py]
      APR[approvals.py]
    end
    subgraph LLM["llm/"]
      RES[resilient.py<br/>retry, breaker, fallback]
      PROV[openai_compat.py, anthropic.py<br/>mock.py, replay.py]
    end
    TOOLS[tools/<br/>read, write, edit, bash, ls, grep<br/>todo, refs, mcp, demo]
    OBS[observe/<br/>trace, replay, evals]

    Clients --> H
    H --> LOOP
    H <--> SM --> ST
    LOOP --> CTX --> SL --> ST
    LOOP --> EX --> PERM & HOOK & APR
    EX --> TOOLS --> WS
    LOOP --> BG
    LOOP --> RES --> PROV
    SUB --> H
    OBS --> ST
```

Dependencies point one way. Consumers know only `Harness` and events. `Harness` assembles a `RunContext` and hands it to `AgentLoop`, and the loop reaches storage, the model, tools and policy through that context. There are no globals. In a test, replacing any part (a scripted model, in-memory storage, a custom permission policy) means changing a constructor argument.

## The timeline of one run

The example is typing `创建 hello.py` ("create hello.py") in the Web UI, in default permission mode:

```mermaid
sequenceDiagram
    autonumber
    participant UI as Web UI
    participant API as server/app.py
    participant H as Harness
    participant L as AgentLoop
    participant M as Model
    participant X as ToolExecutor
    participant DB as Store

    UI->>API: POST /api/sessions/{sid}/runs
    API->>H: start_run()
    H->>DB: create_run(created) then CAS queued
    H-->>API: run (a background task starts driving the loop)
    UI->>API: GET /api/runs/{rid}/events (SSE)
    H->>DB: CAS queued to running
    L->>DB: append user message
    L->>M: stream(system + tools + messages)
    M-->>L: thinking / text / tool_call deltas
    L->>DB: append assistant message (checkpoint anchor)
    L->>X: run_batch([write])
    X->>X: validate, permission=ask, PreToolUse hooks
    X->>DB: create approval, CAS running to waiting_human
    UI->>API: POST /api/approvals/{id} (approve)
    X->>DB: CAS waiting_human to running, shadow-git snapshot, step started
    X->>X: run write (raced against timeout / cancel)
    X->>DB: one transaction: append tool result + step completed
    L->>M: next turn (with the tool result)
    M-->>L: "done"
    L->>L: Stop check (hooks / plan / verify command)
    L->>DB: append assistant message
    H->>DB: CAS running to completed
    H-->>UI: run_end and status events, SSE stream_end
```

Each step maps to code:

| Step | Where | Point to notice |
|---|---|---|
| Create the run | `Harness.start_run()` | One active run per session; input during a run goes through steer or follow-up |
| Drive in the background | `Harness._launch()` then `_drive()` | The run lives in an `asyncio.Task`, independent of the HTTP connection; a heartbeat task starts with it |
| Event dispatch | `EventHub.publish()` | Assigns a monotonic `seq`, writes to the database in batches, then fans out to subscribers |
| Main loop | `AgentLoop.run()` / `_body()` | An async generator that always ends in an explicit state |
| Context | `_maybe_compact()`, `_call_model()` | Estimate usage, compact if needed, clear old results, build the `ModelRequest` |
| Model call | `ResilientProvider.stream()` | Retry, circuit breaker and fallback happen only in this layer |
| Tool execution | `ToolExecutor.run_batch()` / `_run_one()` | Batching and concurrency, permissions, hooks, approval, intent, execution, blob offload, persistence in original order |
| Completion check | `AgentLoop._stop_gaps()` | Unfinished plan, failing verify command or a Stop hook blocks the end and injects the gap |
| End | `Harness._finish()` | Maps the loop's `Outcome` to a state machine transition; if cancel and completion race, the first one wins |

## Four design principles

### 1. Everything is an event

`AgentLoop.run()` is an async generator that yields `Event(type, data, seq)`. The CLI's `TerminalRenderer`, the Web UI's `app.js`, test assertions and a subagent's pass-through to its parent are all consumers of events. The types are defined in `events.py`:

| Category | Events |
|---|---|
| Run | `run_start`, `run_end`, `status` (a state machine transition; offline transitions with no active EventHub, such as create, recover, offline cancel and crash takeover, also write one) |
| Turn | `turn_start`, `turn_end` |
| Message | `message_start`, `message_update` (text / thinking / toolcall deltas), `message_end` |
| Tool | `tool_start`, `tool_update` (progress, partial output, subagent events), `tool_end` |
| Policy | `approval_request`, `approval_resolved` |
| Context | `compaction_start`, `compaction_end` |
| Other | `model_retry`, `notice`, `usage`, `error` |

The naming follows pi's ten AgentEvents (start/update/end for agent, turn, message and tool execution), plus events specific to this runtime: status, approval, compaction and retry.

### 2. Events never get ahead of persisted state

```python
def _append(self, msg: Message) -> Event:
    """消息落盘（检查点）后再发 message_end：事件永远不会领先于持久化状态。"""
    self.rc.log.append_message(msg, self.rc.run_id)
    return self._ev(E.MESSAGE_END, message=msg.to_dict())
```

(The docstring says: emit `message_end` only after the message is persisted as a checkpoint.)

When the UI sees `message_end`, the message is already in the database. If the process crashes now, the history after recovery matches what the UI showed. Tool results follow the same rule: `run_batch` first writes the tool message and the step completion mark in one transaction, and only after the commit emits `tool_end`. `EventHub` obeys it too: it writes a batch of events to the database, then fans out. A client that reconnects reads the gap from the database and never sees an event that existed only in memory.

### 3. History is append-only

Session entries are never modified. Branching only moves the `head` pointer. Compaction appends a compaction entry. Even the system-reminder the harness injects is persisted as a user message, not assembled fresh on each request. This has three payoffs:

- Prefix caching: each request's message list starts with the previous one (`tests/test_loop.py::test_prefix_stability_append_only`).
- Valid thinking signatures: in newer Claude models a thinking block is bound to the full prefix it was generated under, so rewriting history invalidates every later thinking block.
- Faithful replay: the trace records what the model actually saw.

### 4. Every failure becomes an explicit result

| Failure | What it becomes |
|---|---|
| Unknown tool, invalid arguments, permission denied, approval rejected, tool exception, local operation timeout | A tool result with `is_error=True`, which the model reads and corrects |
| An external side effect whose outcome is unknown (cancel grace period expired, non-idempotent external call timed out, process crashed mid-call) | A tool result with `status=unknown` plus a journal record, telling the model to verify before retrying; on recovery it is reconciled through the idempotency key |
| Cancel, pause | `RunCancelled` / `RunPaused`, then `Outcome("cancelled" / "paused")` |
| Budget exhausted, loop detected, repeated failed completion checks | `budget_exhausted` / `loop_detected` / `incomplete`, never disguised as `completed` |
| An unrecoverable model call failure | `failed`; the error goes into the run record and into a message with `stop_reason=error`; `retry()` can continue from history |

The outer `try/except` in `AgentLoop.run()` guarantees that an unexpected exception inside the loop still ends the run as `failed` and keeps the evidence, instead of leaving it in `running` forever.

## Data model

```mermaid
erDiagram
    SESSION ||--o{ ENTRY : "entry tree (parent_id)"
    SESSION ||--o{ RUN : "successive executions"
    RUN ||--o{ STEP : "tool call journal"
    RUN ||--o{ EVENT : "event stream (seq)"
    RUN ||--o{ APPROVAL : "approval objects"
    SESSION ||--o{ BLOB : "offloaded large results (per session)"
    SESSION { string id string head json meta }
    ENTRY { string id string parent_id string kind json payload }
    RUN { string id string status int version int attempt float heartbeat_at }
    STEP { string call_id string status string idem_key }
    EVENT { int seq string type json data }
```

- **Session.meta** freezes the system prompt, the tool set and the model (version pinned), plus session-level grants and the clearing watermark.
- **Entry** is the source of truth for context: a `message` or a `compaction`.
- **Run** carries `version` (incremented on every transition, for optimistic locking), `attempt`, `heartbeat_at` (the lease) and `checkpoint` (the workspace manifest).
- **Step** is a write-ahead log for a tool call: `started` is written before the side effect and the result after, so recovery can tell whether it happened.
- **Event** has a unique `(run_id, seq)` key, used for streaming, resumption after a dropped connection, and audit.

More in [09 State and persistence](/en/reliability/state-persistence).

## Directory layout

```
src/flywhl_harness/
+-- __init__.py          public API
+-- types.py             unified message model: Message / TextBlock / ThinkingBlock / ToolCall / Usage
+-- events.py            event types
+-- cancel.py            CancelToken: cooperative cancellation
+-- config.py            config dataclass tree (flywhl.toml + environment variables)
+-- prompts.py           system prompt, AGENTS.md, skill list, compaction template
+-- agent/               loop, executor, context, compaction, budget, inbox, subagent, run_context
+-- llm/                 base, models, openai_compat, anthropic, mock, replay, resilient
+-- tools/               base, schema, registry, fs, shell, extra, demo, mcp
+-- policy/              permissions, bash_analyzer, hooks, approvals
+-- runtime/             manager, store, session, state, workspace, retry, scheduler, dispatch
+-- observe/             trace, replay, evals
+-- server/app.py        FastAPI: REST + SSE
+-- web/                 index.html, app.js, md.js, style.css (no build step)
+-- cli.py               the flywhl command
```

The directory listing is abridged: `tools/` also holds `confine.py`, `egress.py`, `sandbox.py` and `regex_search.py`, `policy/` holds `fatigue.py`, `sensitive.py` and `supply_chain.py`, `llm/` holds `profiles.py` and `doctor.py`, `observe/` holds `export.py`, and `web/` has `boot.js`, `lib/` and `views/`.

For a suggested reading order of the source, see the [source map](/reference/source-map) (Chinese). Next: [04 The agent loop](/en/core/agent-loop). [Read the full chapter in Chinese](/guide/architecture).
