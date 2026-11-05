# 16 Hooks

*Summary. [Read the full chapter in Chinese](/governance/hooks).*

## The problem

A line in the system prompt such as "run the tests after editing" is followed only when the model notices it. A long context dilutes it, and a web page saying "ignore previous rules" can override it. A rule you cannot afford to have the model forget has to be code that runs every time a lifecycle point is reached. Hooks are that code: they can block, rewrite, add context, or just observe, and the model can neither see nor edit them.

The chapter's test for where a rule belongs: can you accept "the model forgot this time"? Style preferences stay in the prompt. "Never delete migration files" and "tests must pass before finishing" become hooks.

## How it works

**Eight events.** `HookEvent` defines `session_start`, `user_prompt_submit`, `pre_tool_use`, `post_tool_use`, `stop`, `subagent_stop`, `pre_compact` and `run_end`. The same `HookOutput` field means different things per event. `updated_input` only matters on `pre_tool_use`, `updated_result` on `post_tool_use`, and `additional_context` becomes a `<system-reminder>` (or the compactor's extra instructions on `pre_compact`). Fewer events than Claude Code is deliberate: each one is a contract to keep compatible.

**Serial execution, strictest wins.** `HookBus.run` runs matching hooks one at a time in `priority` order (ascending, default 100). Serial order lets one hook's `updated_input` feed the next. The cost is that a `deny` short-circuits `pre_tool_use`, so an audit hook placed after a blocking hook never sees the denied call. Give audit hooks the lowest priority number, or read `tool_end` events instead. Rewriting hooks need small priorities and checking hooks large ones, otherwise the check sees stale arguments.

**Decisions are normalized.** `normalize_decision` maps `approve` to `allow` and, on `pre_tool_use`, `block` to `deny`. A misspelled value is ignored with a `hook_warning`. Before this existed, a `block` returned from `pre_tool_use` was silently ignored and the tool ran.

**Hooks can only tighten.** The executor evaluates permission rules first, then `pre_tool_use` hooks, and takes the stricter result. A hook returning `allow` cannot override a `deny` or `ask` rule. After a hook rewrites arguments, the executor re-validates the schema and re-evaluates permissions on the final arguments. When a human edits arguments on an approval card, `pre_tool_use` hooks run again, and any hook rewrite after approval goes through permissions again. That last step closes a hole where a hook could turn an approved command into `rm -rf ~`.

**Failure policy.** A hook that raises or times out is fail-open (ignored, warning recorded) or fail-closed (immediate block; for `post_tool_use`, earlier redaction is kept and output no hook replaced is withheld from the model). In-process hooks default to fail-open. Command hooks from `flywhl.toml` default per event: `pre_tool_use` fail-closed, the rest fail-open. Fail-open is not silent: warnings are published as `hook_warning` notices at every hook point.

**Stop hooks verify completion.** `AgentLoop._stop_gaps` collects unfinished todos, a failing verify command and Stop-hook blocks. Any gap keeps the run going, up to `max_stop_blocks` (default 3). After that the run ends `incomplete` rather than pretending to be `completed`, so an unreachable check cannot loop forever.

**Command hooks.** `CommandHook` follows the Claude Code protocol: JSON on stdin, exit 0 for success, exit 2 to block with stderr as the reason, anything else is an error handed to the fail policy. It differs in a few places: hooks run serially, event names are lowercase with underscores, and only the command hook type exists. Command hooks run inside the OS sandbox with secret-looking environment variables stripped. On timeout the whole process group is killed (`start_new_session=True` plus `os.killpg`), because cancelling the coroutine alone left the `sleep` child running.

**Hooks are a supply-chain surface.** A hook runs outside the permission system and can read credentials on disk. A cloned repository's `flywhl.toml` could register one. Project hooks are therefore only registered after the user trusts that file by path and content hash, and command hooks re-check the script content before each run.

## Lessons the chapter records

- A fail-open hook turned a serialization bug into a silent bypass. `to_json` once used `dataclasses.asdict`, which tried to deep-copy the SQLite connection in `context`, raised, and let the `rm` through. The unit test missed it because it built a `HookInput` without `context`. Test hooks through the real executor path.
- Redaction in `post_tool_use` covers what the model sees and `tool_end`, not the streamed `tool_update` events that were already published.
- Checking command text for a file name is easy to evade (`sed -i … feature_*.json`). The long-running example therefore also uses a Stop hook that checks the resulting state.

## Key code

| Where | What it does |
|---|---|
| `policy/hooks.py` `HookEvent`, `HookInput`, `HookOutput` | The eight events and the data contract |
| `policy/hooks.py` `HookBus.run()` | Priority order, chaining, timeout, fail policy, aggregation |
| `policy/hooks.py` `normalize_decision()` | Maps aliases and rejects unknown values with a warning |
| `policy/hooks.py` `CommandHook` | Out-of-process protocol, environment, process-group kill |
| `policy/permissions.py` `strictest()` | `allow < ask < deny` aggregation |
| `config.py` `HookConfig` | `[[hooks]]` entries, with per-event fail-closed default |
| `agent/loop.py` `_stop_gaps()` | Stop checks and the `max_stop_blocks` cap |

## Related chapters

- [17 Permissions and human approval](/en/governance/permissions-hitl) holds the rules hooks may only tighten.
- [18 Workspace and sandbox](/en/governance/workspace-sandbox) confines command hooks.
- [19 Security](/en/governance/security) covers hooks as a supply-chain risk.
