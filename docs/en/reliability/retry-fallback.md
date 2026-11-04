# 13 Retries, circuit breaking and fallback

*Summary. [Read the full chapter in Chinese](/reliability/retry-fallback).*

## The problem

A run makes dozens of model calls and hundreds of tool calls, so some will fail. "Back off and retry" is not enough here, for three reasons:

- Requests are large and billed by token. Retrying one that can never succeed, such as an overlong context, wastes money.
- Output is streamed. A failure can arrive after half the text reached the UI and a `tool_use` has half its arguments.
- Layers stack. If an SDK, a gateway and the harness each retry three times, one outage becomes 27 calls.

The rule is to classify first, then pick one of four responses: retry the same request, change the input, switch model, or give up.

## How it works

**Classification.** `classify_http_error()` in `llm/base.py` maps status and body to an `ErrorKind`. Branch order is priority: 503 and "overloaded" in the body become `overloaded`, and context overflow (matched by vendor-specific phrases in `_OVERFLOW_HINTS`, on 400, 413 or 422) must come before the generic 4xx so it does not become `bad_request`. Only `rate_limit`, `overloaded`, `server`, `timeout` and `network` are in `RETRYABLE_KINDS`. Unrecognised client errors become `unknown` and are neither retried nor counted by the breaker, so the system does not report a parse failure as an auth failure. Provider adapters only classify. `AnthropicProvider` sets `max_retries=0` so the harness is the single retry layer.

**Backoff.** `RetryPolicy.backoff()` uses full jitter: `uniform(0, min(max_delay, base_delay * 2^(n-1)))`, with defaults of 0.5 and 20 seconds and `max_attempts=4` including the first try. A `Retry-After` value acts as a floor, so many clients told "come back in 3 seconds" wake together. The chapter suggests adding a little jitter above the floor. `RetryPolicy.deadline` is an absolute time: if the remaining time is less than the delay, retrying stops and the error goes to fallback.

**Circuit breaker.** `CircuitBreaker` (default 5 consecutive failures, 30 second reset) derives its state from `opened_at` and an injectable clock, and `allow()` admits exactly one probe in `half_open`. Only retryable errors count as failures. The subtle part is the probe slot. If a probe ends in a non-retryable error, a cancel or a closed generator, neither `success()` nor `failure()` runs and the breaker would stay half-open forever. Two guards prevent that: `release()` is called on those paths, and a slot older than `reset_timeout` expires on its own.

**`ResilientProvider`** (`llm/resilient.py`) is itself a `Provider`, so the loop does not know it exists. It has an outer loop over the fallback chain and an inner retry loop, so a short blip is retried on the primary model before traffic moves to a weaker one. Models whose breaker is open are skipped. Fallback is allowed for `rate_limit`, `overloaded`, `server`, `timeout`, `network`, `not_found` and `billing`. `auth` falls back only to a different provider, since the same vendor means the same key. `bad_request` and `context_overflow` are raised as is. Each switch emits a `fallback` notice. The chain comes from config, for example `fallback_models = ["deepseek:deepseek-v4-pro", "dashscope:qwen3.8-flash"]`, and breakers are shared by all runs.

**Streaming retry.** Provider APIs cannot resume mid-generation, so the whole request is resent. The `retry` event carries `discard_partial` when anything was already emitted, and the loop clears its partial-text buffer and publishes `model_retry` so the UI can discard what it rendered. A half-built tool call needs no cleanup because each attempt uses a fresh `MessageBuilder`.

**Changing the input instead of retrying:**

| Case | Handling |
|---|---|
| Context overflow | `_call_model()` raises `_ContextOverflow`, `_body()` force-compacts and reruns the turn, once per turn. A second overflow fails the run |
| `max_tokens` with tool calls | The whole batch is not executed; each call gets a `truncated` result so the model re-plans in smaller steps |
| `max_tokens` with text only | A reminder asks the model to continue; at most 3 continuations per run, then a `truncated` warning |
| Transient tool error | `ToolExecutor._invoke()` retries `TransientToolError` up to 3 attempts (0.4 s, 0.8 s backoff) only for read-only or idempotent tools, and only if run deadline allows |
| External non-idempotent tool timeout | Recorded as `unknown`, with an instruction to verify before retrying |

## Not wired up, per the chapter

- `create_provider()` builds `RetryPolicy(max_attempts=cfg.max_retries)` with no `deadline`, so model retries are not bounded by the run's wall clock.
- `RetryBudget` (retries capped to a ratio of requests, with a floor) exists and `retry_async()` accepts it, but `ResilientProvider` does not use it.
- Token buckets, priority queues and bulkheads are not implemented. The harness handles being rate limited, not avoiding it.
- Usage from a failed streaming attempt is lost, so the final message only counts the successful attempt.

Fallback changes behavior, not just availability: tool-calling habits, context window and prefix cache all differ on the backup model, and the chain should hold only models that were evaluated. Each assistant message records the model that actually served it, except failed or cancelled messages, which carry the primary model's name.

## Key code

| Where | What it does |
|---|---|
| `llm/base.py` `classify_http_error()`, `ErrorKind`, `RETRYABLE_KINDS` | Error classification |
| `runtime/retry.py` `RetryPolicy` | Full-jitter backoff and deadline |
| `runtime/retry.py` `CircuitBreaker` | Three states, single probe, `release()` |
| `runtime/retry.py` `RetryBudget`, `retry_async()` | Retry ratio cap and the generic retry helper |
| `llm/resilient.py` `ResilientProvider.stream()` | Chain, retries, breakers, fallback rules |
| `llm/__init__.py` `create_provider()` | Builds the chain from config |
| `agent/loop.py` `AgentLoop._body()` / `_call_model()` | Overflow compaction, truncation handling, `retry` events |
| `agent/executor.py` `ToolExecutor._invoke()` | Tool-level transient retry and deadline-aware timeouts |
| `tools/base.py` `TransientToolError` | Marks a retryable tool failure |

## Related chapters

- [07 Context engineering](/en/core/context-engineering) covers the compaction that overflow recovery triggers.
- [12 Cancellation semantics](/en/reliability/cancellation) covers how backoff sleeps and probes are interrupted.
- [14 Idempotency and side effects](/en/reliability/idempotency) explains why only idempotent tools are retried.
- [15 Streaming, steering and resumption](/en/reliability/streaming) covers how the UI handles `model_retry`.
