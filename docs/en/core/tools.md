# 06 The tool system (ACI)

*Summary. [Read the full chapter in Chinese](/core/tools).*

## The problem

Every read or write the model makes on the world goes through a tool, so tool design sets a ceiling on the agent. A tool is an interface for a reader that cannot browse docs or debug. It sees only `name`, `description` and a parameter schema, and it pays for every byte of the result on every later turn. This is what the SWE-agent paper calls the Agent-Computer Interface (ACI).

The harness also has to decide, for each call, whether it can run in parallel, be retried, needs approval, or can be interrupted. If each tool answered those questions itself, the policies would drift apart.

## How it works

**Declarative tools.** A `Tool` states what it is: `effect`, `interruptible`, `idempotent`, `timeout`, `untrusted_output`. The harness reads these and decides concurrency, retry, approval, cancellation and recovery in one place. `effect` has three levels: `READ`, `WRITE` and `EXTERNAL`. Interruptibility is a separate axis. A tool can be `READ` and still not safe to cut off once the request is sent. After a side effect has been sent, "no reply" does not mean "did not happen", so a timed-out non-idempotent `EXTERNAL` call is reported as `unknown`, not `timeout`.

**Dynamic level.** `BashTool.effect_for()` classifies the command at call time (`ls` is read, `pytest` writes, `git push` is external). The executor classifies again after hooks and approval have run, because either can rewrite the arguments. The level must describe what actually executes.

**Schemas from signatures.** The `@tool` decorator turns an async function into a tool: name from the function, description from the docstring, schema from the annotations, always with `additionalProperties: false`. An invented parameter name gets an error back instead of being silently ignored.

**Safe parallelism.** `run_batch` splits the model's calls into consecutive batches. Adjacent read-only calls form one parallel batch, capped by `parallel_tool_limit` (default 8). Every other call runs alone. Moving reads ahead of a write could return stale content, so only adjacent reads merge. Results are persisted in call order even when they finish out of order, which keeps history deterministic for replay and prefix caching.

**Every failure is a readable result.** Unknown tool, invalid JSON, schema violation and a tool exception each come back as an `is_error` result that says what is wrong and how to fix it. A message cut off by `max_tokens` gets a result saying the call was not executed, because a truncated `write` would otherwise run with half its content. `validate()` is a small JSON Schema subset that returns errors as text. It coerces only unambiguous mistakes (`"5"` to `5`, a single value to a one-element list, a JSON string to an array) and rejects `1.5` for an integer.

**Large results.** The order is: the tool limits output itself, then truncates explicitly with a pointer to the rest, then the harness steps in. `read` keeps the head (2000 lines or 50KB) and prints the next `offset`. `bash` keeps the tail (400 lines or 30KB). `shape_tool_result` is the generic fallback: past `tool_result_max_tokens` (6000) it stores the full text in `BlobStore` under a content-addressed handle and leaves a 60-line head and 20-line tail preview. The model reads more with `read_ref` or `search_ref`. Handles are scoped to the session, so another session cannot read them. The same function also escapes `<system-reminder>` tags in tool output, so a web page cannot pose as the harness.

**File safety.** `write` and `edit` write to a temp file, `fsync`, then `os.replace`, so a killed process leaves the old or the new file. They refuse a file whose mtime changed since the last read in this run, and they decode strictly as UTF-8 instead of rewriting undecodable bytes. `edit` is exact string replacement. A miss or a non-unique match returns line numbers, not a guess.

**Bash details.** It runs in its own process group, ends it with SIGTERM then SIGKILL after a 2 second grace, closes stdin, strips environment variables whose names look like secrets, and decodes UTF-8 incrementally while streaming output. It sets `manages_timeout`, so the executor leaves it room to return partial output.

**Web tools.** `web_search` (DeepSeek Search by default, Bocha and self-hosted SearXNG as failover) and `web_fetch` (DeepSeek Fetch, or `direct` from this machine) are off by default; when off they are not registered and requests are byte-identical to before. Each session picks `off`, `search` or `fetch` before it starts, clamped to what the user config and available backends allow, and can only tighten afterwards: the tool list stays pinned and the executor refuses calls above the current level. Both tools are `read` and `egress`, their output taints the session, and search results carry `[n]` numbers, URLs and a dated header. `fetch_scope` (`cited`, `ask` by default, `public`) decides which URLs can be read without asking, matching full URLs against what the user typed or searches returned. Searches and fetches have per-run caps and their per-call price counts toward `max_cost_usd`, shown separately as `tool_cost_usd`. With `[web] native = true` and a confirmed vendor (Anthropic's `web_search` server tool), the vendor searches instead: server blocks are kept and echoed only to the same endpoint, and counts and sources are read from the response. Replay uses recorded results, and a counterfactual fork stays offline unless `allow_web` is passed.

`todo_write` has no table. The plan is the arguments of the last successful `todo_write` call in the history, so branches and recovery stay consistent.

## Key code

| Location | What it does |
|---|---|
| `tools/base.py` `Tool`, `ToolResult`, `ToolContext`, `Effect` | The declarative tool contract and its only way to reach the outside |
| `tools/schema.py` `validate()`, `_coerce()`, `schema_from_signature()` | Schema subset, safe coercion, schema from annotations |
| `agent/executor.py` `ToolExecutor.run_batch()` | Batching, bounded parallelism, ordered persistence |
| `agent/executor.py` `_prepare()`, `_invoke()`, `_race()` | Pre-execution failures, retry rules, timeout and cancel handling |
| `agent/context.py` `ContextManager.shape_tool_result()` | Externalizes oversized results as blob handles |
| `runtime/store.py` `BlobStore` | Content-addressed, scope-isolated storage |
| `tools/fs.py` `_atomic_write()`, `_check_stale()` | Atomic writes and stale-read protection |
| `tools/shell.py` `BashTool`, `_terminate()` | Process group kill, streaming, dynamic effect |
| `tools/extra.py` `latest_todos()` | Rebuilds the plan from history |
| `tools/__init__.py` `default_tools()` | Fixed registration order of the ten built-in tools; web tools come last when enabled |
| `tools/web.py`, `net/search.py` `WebService` | Web tool specs and `fetch_scope`; backend failover, breaker, cache, pricing |
| `runtime/capabilities.py`, `llm/native.py` | Session web capability; vendor-side search declarations |

## Related chapters

- [05 Messages and models](/en/core/messages-and-models) defines the tool_call and tool_result pairing this chapter relies on.
- [07 Context engineering](/en/core/context-engineering) covers what happens to stored handles when old results are cleared.
- [14 Idempotency](/en/reliability/idempotency) covers `run_id:call_id` keys and reconciliation.
- [17 Permissions and human approval](/en/governance/permissions-hitl) consumes `effect` and the diff previews.
