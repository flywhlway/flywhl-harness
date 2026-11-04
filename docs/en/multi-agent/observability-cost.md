# 23 Observability and cost

*Summary. [Read the full chapter in Chinese](/multi-agent/observability-cost).*

## The problem

Two questions arrive soon after launch: what is the agent doing right now, and why did this month cost so much. Answering them needs three things: traces in a format other tools read, a short list of metrics computed from data the harness already stores, and cost numbers that are not double-counted.

## How it works

**Traces.** `to_otel_spans()` turns one run's event stream into OpenTelemetry GenAI-style span dicts, without the OTel SDK. A root `invoke_agent flywhl` span covers the run. Each assistant message becomes a `chat` span (from `message_start` to `message_end`), and each tool call becomes an `execute_tool` span (from `tool_start` to `tool_end`, with `error.type` set on errors). Chat and tool spans are siblings under the root. IDs are derived by hashing the run ID and the message or call ID, so exporting the same run twice gives identical IDs and a backend can deduplicate. Prompts, replies and tool arguments are left out by default. They live in the trajectory's `messages`, where access control can differ from the trace backend's.

Known gaps against the conventions:

- A child run gets its own `trace_id` and its root span has no parent, so subagents are not in the parent's trace.
- A `trace_id` derived from the run ID cannot join an upstream trace (no W3C `traceparent` input).
- Cache-write tokens are not exported, only cache-read.
- The root span's usage includes subagents, so sum root spans or sum `chat` spans, not both.
- Failed runs get no `error.type` on the root span.

**Metrics.** The chapter lists about 17 indicators, all computable from the `runs` and `events` tables without new instrumentation: state distribution, runs whose heartbeat is stale, time to first token, cost and turns per run, cache hit rate, tool error rate, retry and fallback rate, loop-guard hits, compaction frequency, approval wait, budget-exhaustion rate and premature stop rate. Alert on change against a baseline ("3x last week"), not absolute values, because task sizes vary so much. Every status change, including offline ones such as `queued`, `resume` and `recover`, is a `status` event, so the state history is readable from `events` alone.

**Cost accounting.** `Usage` holds `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` and `cost_usd`, taken from the provider response. `compute_cost()` multiplies each of the four by its own per-million price from the `ModelInfo` table. That is only correct if the four counts do not overlap, so the OpenAI-compatible adapter subtracts the cached part out of `prompt_tokens` (the cached count sits in a different field for different vendors). Skipping the subtraction bills cached tokens twice, and the error grows as the cache works better.

The price table is maintained locally and will drift from invoices, so reconcile periodically. Some cases bypass it: providers that report cost (such as OpenRouter) are trusted, DeepSeek is priced at the peak rate and so is an upper bound, and CNY-priced models convert at `CNY_PER_USD` (override with `FLYWHL_CNY_PER_USD`). A model missing from the table costs 0, which would silently disable `max_cost_usd`, so a run with a cost budget and an unpriced model emits a `notice` with `code=unpriced_model`.

**Attribution traps.** `runs.usage` for a parent run already includes its subagents' usage, so summing every row of `runs` counts subagents twice. Sum only root runs (`parent_run_id IS NULL`), or sum assistant `message_end` usage plus compaction calls (compaction is charged through `charge_model()` but writes no assistant message). Runs have no tenant field. Put one in the run config or session `meta` and group by it.

**Keeping the prefix stable.** Prefix caching is the first cost lever, so the harness protects it:

- The system prompt and tool set are frozen in session `meta` at creation.
- Reminders are persisted as appended user messages, not spliced into each request.
- The clearing watermark only moves forward, and resets to zero after compaction.
- Models marked `append_only` skip result clearing and rely on compaction.
- `test_prefix_stability_append_only` asserts that consecutive requests have identical system and tools and that each message list starts with the previous one.

The model is not pinned, so an old session can continue under a new model, and then cost, turns and hit rate are not comparable across the switch (a `model_switched` notice marks it).

**Cost spike playbook.** When many runs sit in `running` and cost jumps, separate stale-heartbeat runs (no tokens spent, but blocking their sessions) from fresh ones (spending tokens, maybe looping). Then check in order: repeated tool-call signatures and `loop_guard` notices, `model_retry` and fallback events (look for discarded partial output), more than one compaction in a run, and a sudden fall in cache hit rate. The chapter has a SQL query for each step. To stop the bleeding, use `BudgetConfig.max_cost_usd` or `max_total_tokens` (both unset by default), pause or cancel runs, and rely on the circuit breakers in `ResilientProvider`. Budgets are checked at turn start, so one call can overshoot and parallel subagents can together exceed the limit. Leave headroom.

**Not implemented.** There is no OTLP exporter and no metrics endpoint. The chapter sketches a `RUN_END` hook that posts spans to an OTel Collector, and a choice between querying the database, turning events into counters, or sending spans to an LLM observability platform. These are marked as sketches.

## Key code

| Where | What it does |
|---|---|
| `observe/trace.py` `to_otel_spans()` | Event stream to `invoke_agent`, `chat` and `execute_tool` spans |
| `types.py` `Usage` | Four token counts plus cost |
| `llm/models.py` `compute_cost()` | Price per million tokens for each of the four counts, with CNY conversion |
| `llm/openai_compat.py` | Converts `prompt_tokens` to the non-overlapping `Usage` |
| `agent/context.py` `apply_clearing()` | Forward-only watermark; disabled for `append_only` models |
| `agent/loop.py` `_inject_reminders()`, `_maybe_compact()` | Persisted reminders; compaction resets the watermark and is charged to the budget |
| `agent/budget.py` `Budget.check()` | Per-turn limit check |
| `llm/resilient.py` `ResilientProvider` | Per-provider circuit breakers on the fallback chain |
| `tests/test_loop.py` `test_prefix_stability_append_only` | Guards the append-only invariant |

## Related chapters

- [20 Subagents](/en/multi-agent/subagents) explains why child usage rolls up into the parent.
- [22 Trajectories, replay and evaluation](/en/multi-agent/trajectory-eval) defines the trajectory the spans ship in.
- [07 Context engineering](/en/core/context-engineering) covers clearing and compaction.
- [24 Long-running harnesses](/en/advanced/long-running) covers heartbeats and recovery at longer horizons.
