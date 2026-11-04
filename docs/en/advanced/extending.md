# 30 Extension guide

*Summary. [Read the full chapter in Chinese](/advanced/extending).*

## The problem

You want to add a tool, a model, a guard or a storage backend without forking the loop. The loop, permissions, hooks and journal only know a few interfaces, so each extension point is a constructor argument or a config entry. The signature is `Harness(config, *, provider=None, model=None, tools=None, store=None, hooks=None, approval_resolver=None, worker_id=None)`. Anything you do not pass falls back to the default: `create_provider`, `default_tools`, the SQLite `Store` and an empty `HookBus`.

## Extension points

| Goal | What to implement | Details that matter |
|---|---|---|
| Add a read-only or simple tool | A function with the `@tool(effect=..., idempotent=...)` decorator, registered on the `ToolRegistry` | The name comes from the function, the description from the docstring, the schema from the signature (`schema_from_signature`). `Annotated[type, "text"]` documents a parameter. A parameter named `ctx` receives `ToolContext` |
| Add a tool with external side effects | Subclass `Tool`: `effect`, `interruptible`, `idempotent`, `execute()`, and `reconcile()` | `ctx.idempotency_key` is `run_id:call_id` and survives restarts. On recovery the executor calls `reconcile()` for steps with an intent but no result. Return errors as `ToolResult.error(...)`, not exceptions |
| Tune a tool's behavior | `effect_for(args)`, `interruptible_for`, `untrusted_for`, `timeout`, `manages_timeout`, `preview`, `permission_target` | `effect` decides concurrency, default approval, snapshots and whether recovery may redo the call. `preview` feeds the approval card |
| Add a model on a Chat Completions-compatible API | One `ProviderProfile` entry in `llm/profiles.py` | No code. Check with `flywhl doctor <provider>:<model>` |
| Add a model on another protocol | Subclass `Provider` and implement `stream(req, cancel)` | The last event must be `done` with the full message. Build with `MessageBuilder`, run `prepare_history`, classify errors with `ErrorKind`, compute `cost_usd` yourself, map truncation to `StopReason.MAX_TOKENS`. The provider does not retry |
| Register model facts and a fallback chain | Add a `ModelInfo` to `MODELS`; wrap providers in `ResilientProvider` | Passing `provider=` skips `create_provider`, so nothing wraps it for you. Set `append_only=True` if history must never be rewritten |
| Add a guard or audit | `HookBus.on(HookEvent..., matcher=, priority=, fail_closed=)` in process, or `[[hooks]]` in `flywhl.toml` as an external command | `pre_tool_use` decisions can only tighten. `updated_input` is validated and judged again. An approver's edits re-run `pre_tool_use`. Guards should set `fail_closed` |
| Set policy | `[permissions]` mode and rules `decision:tool(glob)` | Order is deny, ask, allow. `MANAGED_BASELINE` comes first. Catastrophic commands are denied in every mode. A compound bash command is allowed only if every segment is |
| Connect an MCP server | `[mcp_servers.<name>]` with `command` and `args` | Tools are external and tainted by default. Mark read-only tools in `read_only_tools` |
| Replace storage | A class with the same methods as `Store`, returning the same record types | `Store` is a concrete class. It needs nestable `tx()`, atomic `append_entry`, CAS in `cas_run`, a conditional `heartbeat`, `step_started` that bumps `attempt` on a repeat, and a deduplicating `append_events`. Its methods are synchronous |
| Run tools elsewhere | A `Tool` registered under the same name with `replace=True` | Keep the name `bash` so command analysis and rules still apply, and copy the `effect_for`, `untrusted_for` and `interruptible_for` behavior. Local path checks, shadow git and the workspace manifest do not follow you to a remote filesystem |
| Consume events | `Harness.subscribe(run_id, after_seq)` | Backfills from the store, then goes live, deduplicated by `seq`. It is per run; there is no global stream |
| Test it | `ScriptedProvider` and `tests/conftest.py` `make_harness` | Scripted models test harness logic only. Assert on events (`tool_ends`), not printed output |

## Configuration notes

Config loads in this order: defaults, user config `~/.flywhl/config.toml`, project config, environment variables, explicit arguments. Unknown keys are an error. A `flywhl.toml` in the launch directory may come from a freshly cloned repo, so settings that execute code or widen approval (`hooks`, `mcp_servers`, `verify_command`, `allow` rules, `bypass`, and similar) take effect only after the file's content is trusted. Trust is recorded by absolute path and SHA-256 in `~/.flywhl/trusted.json`. Library code ignores untrusted settings and warns; pass a `trust` callback to `HarnessConfig.load` to confirm them.

## Key code

| Where | What it does |
|---|---|
| `tools/base.py` `Tool`, `FunctionTool`, `tool()` | Tool interface and decorator |
| `tools/__init__.py` `default_tools()` | The default registry |
| `llm/profiles.py` `ProviderProfile` | Table entry for compatible providers |
| `llm/resilient.py` `ResilientProvider` | Retry and fallback wrapper |
| `policy/hooks.py` `HookBus`, `HookEvent`, `CommandHook` | The eight hook points and external hook runner |
| `policy/permissions.py` `MANAGED_BASELINE` | Rules that always come first |
| `runtime/store.py` `Store` | The storage contract to reimplement |
| `config.py` `HarnessConfig` | Every setting, with defaults |
| `tests/conftest.py` `make_harness` | Test harness with a scripted model |

## Related chapters

- [05 Messages and model access](/en/core/messages-and-models) explains the provider profile fields.
- [16 Hooks](/en/governance/hooks) and [17 Permissions and human approval](/en/governance/permissions-hitl) cover the policy extension points in depth.
- [27 Protocols: MCP, A2A and Skills](/en/advanced/protocols) covers MCP server setup and trust.
- [09 State persistence](/en/reliability/state-persistence) covers what `Store` guarantees.
