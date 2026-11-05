# 05 Messages and model access

*Summary. [Read the full chapter in Chinese](/core/messages-and-models).*

## The problem

The agent loop should not know that OpenAI says `finish_reason` and Anthropic says `stop_reason`, or that a tool result is its own message in one protocol and a block inside a user message in the other. Left to leak, these differences end up as `if vendor == ...` branches across the loop, and the failures they cause are quiet:

| Difference | What goes wrong if it leaks |
|---|---|
| OpenAI-compatible vendors disagree on where thinking text comes from, whether to send it back, and what the on/off switch is called | Per-vendor branches in the provider, or a 400 on the second turn |
| 429 can mean rate limit, but one vendor also wraps "out of balance" in it | Four pointless backoff retries on an error that will never clear |
| Both SDK and harness retry | Retries multiply, and the ones inside the SDK are invisible to the UI, the budget and the circuit breaker |
| A system prompt that is regenerated per request | The cached prefix breaks and every request is billed at full price |
| A tool result without a matching call, or the reverse | The API rejects the request |

## How it works

**One message model.** `types.py` defines three roles (`user`, `assistant`, `tool`) and three block types (text, thinking, tool call). The system prompt is not a message: it travels in `ModelRequest.system`, so refreshing it never rewrites the start of the history, and the message list can stay strictly append-only. A `tool` message splits its payload into `content`, which the model sees, and `details`, which only the UI and audit see (the `write` tool puts the diff in `details`). Both converters read only the text, so `details` cannot reach the model by construction. A `ToolCall` keeps `raw_arguments` and `parse_error`, so a malformed argument string goes back to the model with the reason instead of being swallowed. `meta.kind` records where a user-role message came from (`prompt`, `steer`, `follow_up`, `reminder`, `compaction`, `resume_note`, `stop_feedback`, `wrap_up`). Each `stop_reason` has one defined treatment: `max_tokens` marks a batch of tool calls `truncated` and runs none of them, while `error` and `refusal` messages are stored for audit but skipped when the next request is built.

**An anti-corruption layer plus a governance layer.** `ResilientProvider` (retry, circuit breaker, fallback chain) wraps `AnthropicProvider` or `OpenAICompatProvider`. Providers share one stream contract: events end with a `done` carrying a full `Message`, recoverable failures are raised as `ProviderError` with an `ErrorKind`, and providers never retry. The comment gives the reason: three layers each retrying three times is 27 times the traffic.

- **Partial JSON.** `MessageBuilder` accumulates tool-argument fragments by block index and parses only when the block closes. A partial tool call is dropped on abort, and a partial thinking block is dropped because it has no signature.
- **Error classes decide policy.** `classify_http_error()` checks billing hints in the body before the status code, because one vendor reports "insufficient balance" as 429 and DeepSeek reports an unknown model as 400. Context overflow has no standard status, so it is detected by phrases such as `prompt is too long`; it is not retried as is, and the loop compacts first. `RETRYABLE_KINDS` (retry in place) and `_FALLBACK_KINDS` (switch model) are separate tables on purpose. These cases are recorded as fixtures and asserted in `tests/test_providers.py`.
- **`prepare_history()`.** It skips `error`/`refusal` assistant messages, drops orphan tool results, and adds a synthetic error result for a call that lacks one mid-history. It leaves the trailing unpaired call alone, since the loop is still handling it. Every rule depends only on the message and what came before, so the output for a prefix is always a prefix of the output for the longer history, and the cache survives.
- **Protocol differences.** Anthropic merges all `tool_result` blocks of a turn into one user message, ahead of any text; OpenAI wants one `tool` message per result. For usage, OpenAI's `prompt_tokens` includes cache hits and Anthropic's `input_tokens` does not, so the OpenAI path subtracts the cached part to avoid billing it twice.
- **Vendor profiles.** `ProviderProfile` in `llm/profiles.py` holds base URL, key variable, how reasoning arrives and is echoed back, and the thinking switch. Adding a compatible vendor is usually one profile line, then `flywhl doctor` and `tests/live`. DeepSeek requires `reasoning_content` to be sent back when tools are used. MiniMax inlines `<think>` tags, which `ThinkTagSplitter` strips from the start of the message only. A stream that ends without a `finish_reason` is raised as a `network` error, not accepted as complete.
- **Official SDK, `max_retries=0`.** The Anthropic provider uses the SDK's raw stream events, and `_map_error()` maps SDK exceptions onto `ErrorKind`. An unrecognized error is `UNKNOWN`: not retried and not passed off as an auth failure.

**Thinking signatures and prefix caching.** Anthropic thinking blocks must be sent back unchanged. Signatures from another provider are dropped, and `redacted_thinking` is stored inside the `signature` field with a prefix, which keeps the model at three block types. Claude models are flagged `append_only`, so `ContextManager` switches off client-side clearing of old results and leaves compaction as the only way to shrink context. Cache stability comes from a set of constraints: a system prompt and tool set frozen into session meta at creation, stable tool order, reminders persisted as messages, `tool_choice="none"` instead of removing tools at wrap-up, `canonical_json()` for hashing, and a top-level `cache_control`. The cost of freezing is stated: the date in the prompt goes stale in a multi-day session, and switching model invalidates the cache.

**Cost and tool arguments.** `ModelInfo` records context window, prices, `append_only`, `server_fallback`, currency and long-context `tiers`, and `compute_cost()` turns real `usage` into dollars (CNY prices convert at `CNY_PER_USD`, overridable with `FLYWHL_CNY_PER_USD`). A model missing from the table gets a 128K window and zero prices, which would silently disable `max_cost_usd`, so the loop emits an `unpriced_model` notice. Argument safety has five layers: schema design, optional strict decoding, `MessageBuilder`, validation with unambiguous coercion in `tools/schema.py` (`"5"` becomes `5`, `"a.py"` becomes `["a.py"]`), and readable errors from the executor. With `eager_input_streaming` on Anthropic the server no longer validates arguments, so the executor's check is the only guarantee.

**Server-side fallback.** Separate from the client fallback chain: for models with `server_fallback`, the request sends `fallbacks: "default"` so a safety refusal is retried by the server inside the same stream. The provider then emits a `notice` and calls `drop_non_text()` to discard the thinking and tool calls from before the switch. It is sent only to the official endpoint.

## Key code

| Location | What it does |
|---|---|
| `types.py` `Message`, `StopReason`, `MsgKind` | The provider-neutral message model |
| `llm/base.py` `MessageBuilder` | Turns stream deltas into a complete message; parses tool arguments only when complete |
| `llm/base.py` `classify_http_error()`, `ErrorKind` | Maps HTTP errors to a class that decides the policy |
| `llm/base.py` `prepare_history()` | Deterministic repair of tool call and result pairing |
| `llm/openai_compat.py` `to_openai_messages()`, `ThinkTagSplitter` | OpenAI-style conversion and inline `<think>` handling |
| `llm/anthropic.py` `to_anthropic_messages()`, `_map_error()` | Anthropic conversion, thinking signatures, SDK error mapping |
| `llm/profiles.py` `ProviderProfile` | Vendor differences as data |
| `llm/models.py` `ModelInfo`, `compute_cost()` | Capabilities, prices and per-message cost |
| `llm/__init__.py` `create_provider()` | Resolves `<provider>:<model>` and wraps the fallback chain in `ResilientProvider` |

## Related chapters

- [04 The agent loop](/en/core/agent-loop) calls this layer through `_call_model()`.
- [07 Context engineering](/en/core/context-engineering) uses `append_only` and the usage estimate.
- [13 Retry, circuit breaker and fallback](/en/reliability/retry-fallback) covers what `ResilientProvider` does with the error classes.
- [22 Trajectories, replay and evaluation](/en/multi-agent/trajectory-eval) uses the context fingerprint and `ReplayProvider`.
