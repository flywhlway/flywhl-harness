# 15 Streaming, steering and resumption

*Summary. [Read the full chapter in Chinese](/reliability/streaming).*

## The problem

Showing tokens as they arrive is the easy part. A real UI also has to survive these:

- A browser tab refreshes or the network drops while the run keeps producing events.
- A slow client must not stall the agent.
- A single answer can contain a thousand deltas, and writing each one to the database is heavy write amplification.
- The user types while the agent is working, and input injected at the wrong moment breaks `tool_call` / `tool_result` pairing.
- A stream fails halfway, after text was shown and a tool call has half its arguments.

The design keeps the run independent of any connection, numbers every event, and makes reconnecting a replay from the database.

## How it works

**The path of a token.** Provider stream events become `Event`s in `AgentLoop._call_model()`, pass through `Harness._drive()` into `EventHub.publish()`, then to `Harness.subscribe()`, the SSE endpoint in `server/app.py`, and `EventSource` in `web/app.js`. The run executes in a background task started by `_launch()`. A connection is only a subscriber, so refreshing or closing a tab does not touch the run. Only an explicit `POST /api/runs/{rid}/cancel` does.

**SSE rather than WebSocket.** The event stream is one-way, and stop, steer and approve are rare enough for plain `POST`. SSE gives automatic reconnect with `Last-Event-ID` for free. The cost is the browser's per-domain connection limit on HTTP/1.1, so serve it behind HTTP/2.

**`EventHub` numbers, stores, then fans out:**

- `publish()` assigns a monotonic `seq` and buffers the event. Fan-out happens in `flush()`, after `Store.append_events()`. Subscribers therefore never see a `seq` that is not in the database. If it were the other way round, a `kill -9` could leave a client holding `seq` 41 that recovery later reuses for a different event, and the client would drop it as a duplicate.
- Only `message_update` and `tool_update` (`EPHEMERAL_EVENTS`) are batched, flushed after 50 ms or at 64 events. Every other event flushes immediately along with the deltas before it, so database order matches `seq` order.
- After a pause, takeover or crash, the hub starts from `max_event_seq` so a client's `Last-Event-ID` stays valid. Transitions with no live hub are written as `status` events with the next sequence number assigned inside one transaction. A fenced hub drops everything, since the sequence space belongs to the new owner.
- Backpressure: if a subscriber's queue (2000) fills, the hub removes it and queues a `_LAGGED` marker. The producer never blocks. The SSE endpoint adds its own bounded queue (256) between `subscribe()` and the socket, so a slow browser pushes back all the way to the hub. Without that bound the lag path would never trigger.

**`subscribe()` loses and repeats nothing.** The order matters: register the live queue, flush the buffer, read `seq > after` from the database, then drain the queue and drop anything with `seq <= last`. Events flushed after registration reach the queue. Events flushed before it are in the database. Overlap is removed by `seq`. Reading the database first and subscribing second would lose events flushed in between. The `_LAGGED` branch repeats the same sequence for the same reason, and `_CLOSED` triggers a final database read for the tail. In a multi-worker deployment, `subscribe()` only finds runs active in its own process, so cross-process fan-out is left to production work.

**The SSE endpoint.** `Last-Event-ID` takes priority over `?after=`. The first frame is `retry: 1500`, every event carries `id: <seq>`, and a `: ping` comment goes out every 15 seconds so proxies do not close an idle stream. A final `stream_end` event lets the client close deliberately, since a server-side close otherwise looks like a drop and triggers reconnect. Subscription runs in a separate `pump` task, because timing out `wait_for` on the generator itself would cancel it and end the stream after the first heartbeat.

**Three kinds of interruption:**

| | Stop | Steer | Follow-up |
|---|---|---|---|
| Effect | Cancels the run | Injected into the next model call | Injected when the agent would stop |
| When | Next cancellable wait | Safe point, after the current tool batch | End of work |
| Stored as | `aborted` message, synthetic results | user message, `kind=steer` | user message, `kind=follow_up` |

A steer cannot land mid-turn because the only valid place in history is after a whole batch of tool results. "Change direction now" means stop plus new input. The loop checks the inbox twice before ending, because `_stop_gaps()` can run a whole test suite and the user may type during it. `Harness._finish()` handles input still queued at the end: a paused run saves it to the checkpoint and restores it on resume, while any other end state emits an `inbox_dropped` notice that lists the messages. A process crash still loses queued input, because the `Inbox` is in memory until a pause.

**Tool arguments stream, but execution waits.** Anthropic's `eager_input_streaming` lets large arguments arrive as they are generated, at the cost of server-side validation. Three gates compensate: JSON is parsed only when the block closes, tools run only from the complete message in the `done` event, and `_prepare()` validates against the schema. The chapter chooses to wait for the whole message so permission checks and read-only batching see a finished batch.

**Frontend.** `message_update` appends to a draft, rendered once per animation frame. `message_end` replaces the draft with the stored message, and `run_end` reloads the session timeline as the final authority. On `model_retry` with `discard_partial`, the client clears text, thinking and tool cards for that message. The client also dedupes by `seq`, which is safe because the hub only fans out stored events, so one `seq` never names two events.

## Key code

| Where | What it does |
|---|---|
| `runtime/manager.py` `EventHub` | Numbering, batching, store-then-fan-out, `_LAGGED`, `fence()` |
| `runtime/manager.py` `Harness.subscribe()` | Register, flush, replay, dedupe |
| `runtime/manager.py` `Harness._finish()` | Saves or reports unprocessed inbox input |
| `runtime/store.py` `append_events()`, `list_events()`, `max_event_seq()`, `append_event_next()` | Event persistence and sequence continuity |
| `events.py` `EPHEMERAL_EVENTS` | Which events are batched |
| `server/app.py` events endpoint | SSE framing, pump task, heartbeat |
| `agent/loop.py` `AgentLoop._body()` / `_drain_inbox()` | Steer and follow-up injection points |
| `agent/inbox.py` `Inbox` | Steer and follow-up queues |
| `llm/anthropic.py` `AnthropicProvider._kwargs()` | Enables `eager_input_streaming` |
| `web/app.js` `connect()` / `onEvent()` | Reconnect, dedupe, draft rendering |

## Related chapters

- [12 Cancellation semantics](/en/reliability/cancellation) covers the stop path and the fencing that fences a hub.
- [13 Retries, circuit breaking and fallback](/en/reliability/retry-fallback) produces the `retry` events handled here.
- [10 Run lifecycle](/en/reliability/run-lifecycle) defines the transitions that become `status` events.
- [04 The agent loop](/en/core/agent-loop) shows where the inbox is drained.
