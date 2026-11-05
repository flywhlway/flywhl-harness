# 24 Long-running harnesses

*Summary. [Read the full chapter in Chinese](/advanced/long-running).*

## The problem

A task that does not fit in one context window has to be passed across many sessions. Anthropic's write-up on long-running agents names two failures that point in opposite directions and share one cause, which is no reliable handoff between sessions:

| Failure | What happens |
|---|---|
| Doing everything at once | The first session tries every feature, runs out of context and leaves half-working code. The next session spends tokens guessing where it stopped |
| Declaring victory early | A later session sees a lot of code in the repo and decides the project is basically done |

The principle the code states in the docstring of `AgentLoop._stop_gaps` is that completion is a property of the artifact, not of the conversation. "The model said it's done" does not count. "The verification steps for this item ran and passed" does.

## How it works

**The structure from the article.** An initializer session produces `init.sh`, a `feature_list.json` with every item `passes: false` and each item carrying its own verification `steps`, a progress file and a first commit. Coding sessions then start with a fresh context and follow a fixed opening: read `git log` and the progress file, pick the highest-priority failing item, smoke-test with `init.sh`, implement one item, verify it end to end, flip only `passes`, write progress, commit. Both roles use the same harness, tools and system prompt, and only the first user prompt differs. The coding agent may not delete or rewrite items, which closes the cheapest cheat, removing a feature it cannot build. None of this needs a smarter model, only structured handoff files and deterministic acceptance. The same article later splits roles into planner, generator and evaluator with a written "sprint contract" agreed before work starts, since models grade their own work too leniently.

**Rebuilding it with three hooks.** There is no built-in long-running mode. `examples/10_long_running.py` assembles the structure from hooks, which move "must happen" rules out of prompts and into code.

| Hook | Job |
|---|---|
| `session_start` (`orient`) | Records a baseline of (passing count, log lines) and appends the opening routine to the first message as a system-reminder. Advisory only |
| `pre_tool_use` (`guard_feature_list`, `fail_closed=True`) | Lets `feature_list.json` change only in its `passes` fields |
| `stop` (`one_unit_done`, coding sessions only) | Blocks the end of the session until at least one item flipped to passing and one new line was added to the progress log |

The guard compares the list before and after with `passes` stripped from every item, so adding, removing, rewriting or reordering items is denied. Several details close bypasses:

- The path is compared after `resolve()`, so `./feature_list.json` and `sub/../feature_list.json` still match.
- For `edit`, it simulates the full result first (honoring `replace_all`) and applies the same JSON check as `write`.
- A `bash` command that names the file and is not read-only according to command analysis is denied, while `cat` and `jq` pass.
- `fail_closed=True` matters because a hook that throws is otherwise ignored and the write goes ahead.

Hook decisions only tighten permissions, so the guard still holds in `bypass` mode. If an approver edits arguments, the executor re-runs `PreToolUse` on the new ones.

The Stop hook plugs into `_stop_gaps`. When the model replies without tool calls, the loop checks the plan (`verify_todos`), the verify command and Stop hooks, and gathers all gaps. If there are gaps and fewer than `max_stop_blocks` (default 3) blocks so far, it appends a `STOP_FEEDBACK` reminder and continues. Past that limit the run ends as `incomplete`, never as `completed`.

**Limits of the example.** The bash rule matches on strings. `sed -i 's/add/sub/' f*.json` (a glob hides the filename) and a script that rewrites the list are not caught. The chapter's answer is to verify the invariant on the artifact itself at the end, or to keep the list outside the workspace and expose a single "mark passed" tool. Guards that match tool names and command strings cover only known write channels.

**Existing mechanisms that help.** Within a session: plan recitation (`todo_reminder`, after 4 turns without a `todo_write`), plan acceptance, `verify_command`, and compaction (a `pre_compact` hook can require keeping the current feature ID). Across a crash: checkpoint and `recover()`. The budget soft threshold (`soft_ratio`, 0.8) is the last good moment to write progress. One gap: `_wrap_up` forbids tools, so its summary cannot reach `progress.md` and a new session never sees it. The chapter sketches a `run_end` hook that appends it, best effort only.

**Expensive-to-verify domains.** Layer the evidence: simulators, recorded replays, shadow environments, contract tests, human sign-off. Three rules: keep test and replay assets outside the agent's write domain, run cheap checks every session and costly ones only at the "mark as passed" moment, and record which layer passed (`verified: {"sim": true, "hil": false}` rather than one boolean).

**Who writes `passes`.** In the example the agent does, which still relies on its honesty. Safer is a verifier hook that runs each item's `steps` and writes the flag itself while the agent has read-only access, or a human approval for items that cannot be automated. The agent can propose new items in a separate file but not edit the list.

**Not built.** An initializer mode, a session relay driver (the example uses a plain `for` loop), a git commit at session end (the shadow git repo is for snapshots, not the user's history), a per-item verifier registry and a browser verification tool.

## Key code

| Where | What it does |
|---|---|
| `examples/10_long_running.py` `install_hooks()` | The three hooks and the demonstration that tampering is denied |
| `agent/loop.py` `_stop_gaps()` | Gathers plan, verify command and Stop hook gaps before a run may end |
| `agent/loop.py` `_wrap_up()` | Tool-free final summary after a hard budget limit |
| `config.py` `max_stop_blocks`, `verify_todos`, `verify_command` | The Stop acceptance settings |
| `config.py` hook `fail_closed` | Defaults to closed for `pre_tool_use` |
| `agent/context.py` `todo_reminder()` | Plan recitation inside a session |
| `runtime/manager.py` `start_run()` | Runs `session_start` hooks on an empty session |

## Related chapters

- [08 Termination and budgets](/en/core/termination-budget) covers the soft and hard limits.
- [16 Hooks](/en/governance/hooks) covers hook events, decisions and `fail_closed`.
- [11 Checkpoints and crash recovery](/en/reliability/checkpoint-recovery) covers surviving a crash within a session.
- [25 Mapping to durable execution engines](/en/advanced/durable-execution) moves from file handoff to execution-level durability.
