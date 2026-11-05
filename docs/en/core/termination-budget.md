# 08 Termination, budgets and loop guards

*Summary. [Read the full chapter in Chinese](/core/termination-budget).*

## The problem

Stopping an agent is easy. Stopping it honestly is the hard part. A run can end because the model said it was done, a limit was hit, the user cancelled, or something broke. Downstream code reacts differently to each: it keeps the artifact, adds budget, reruns, or ignores the run. If a half-finished run is recorded as `completed`, a pipeline ships a half-finished artifact. If an unmet check is recorded as `failed`, the record of how far it got is lost.

Three failures are common. The model claims done when it is not. The model repeats a call that gets the same answer. A budget runs out mid-task and leaves a pile of intermediate state and nothing a person can pick up.

## How it works

**Distinct end states.** `completed` and `incomplete` come from the natural-finish path. `budget_exhausted` and `timed_out` come from limits. `loop_detected` comes from the loop guard, `cancelled` from the user or a Stop hook with `stop_run`, and `failed` from unrecoverable errors. `paused` is not an end. A rejected approval does not end the run: the model gets a `rejected` tool result and can adjust.

**Budget** is five limits in parallel: turns (default 40), tool calls (200), total tokens, cost and wall clock. Any one trips it. Only the last three are off by default. Token counts include cache reads and writes. `Budget.check()` runs at the top of each round, so a limit can be overshot by up to one round. At `soft_ratio` (0.8) the model gets one reminder per dimension that asks it to finish the key part and leave progress notes. It does not say "stop", which would push toward premature stops. The hard check uses a side-effect-free `hard_verdict()` so a child budget can consult its parent without swallowing the parent's soft warning.

**Wrap-up.** On a hard limit `_wrap_up()` makes one more model call with `tool_choice="none"`. The tool definitions stay in the request so the cached prefix is unchanged. If the model calls a tool anyway, each call gets a `skipped` result and nothing runs. A failure in this call is swallowed, because the real cause is the limit. The turn counter does not count it, but usage does.

**Nested budgets.** `Budget.child()` caps cost and tokens at the parent's remaining amount. Charges propagate up. A parent's hard limit stops its children. `deadline_left()` passes an absolute wall-clock deadline down, so a 120 second tool is shortened when the run has 10 seconds left. Budget counts are saved in the run checkpoint, so pause and resume cannot be used to reset limits. Loop guard state is not saved and starts fresh.

**LoopGuard** compares the call signature (name plus canonical JSON of the arguments) together with a hash of the result, inside a sliding window of 12. Three identical pairs warn, five stop. Other patterns are an A-B-A-B alternation (warn only), six consecutive errors (stop), and six successful edits to one file (warn only). A poll whose result changes is not flagged. A poll that always returns "running" is indistinguishable from a dead loop, so waiting belongs inside a tool with its own timeout.

**Stop checks.** A model saying "done" is a stop request. `_stop_gaps()` checks unfinished todos (`verify_todos`), the `verify_command` exit code, and Stop hooks. If gaps exist, the loop appends them as feedback and continues, up to `max_stop_blocks` (3) times. After that the run ends as `incomplete`, never `completed`. The feedback tells the model to say so honestly if it cannot finish, which gives it an exit other than inventing a success. In `plan` mode the todo and command checks are skipped, because the plan is the product and its items are meant to stay pending.

## Key code

| Location | What it does |
|---|---|
| `agent/budget.py` `Budget` (`check()`, `hard_verdict()`, `child()`, `deadline_left()`) | Limits, soft and hard verdicts, nesting, deadlines |
| `agent/budget.py` `LoopGuard.observe()` | Repeat, ping-pong, error and same-file detection |
| `agent/loop.py` `AgentLoop._wrap_up()` | Tool-free closing summary |
| `agent/loop.py` `AgentLoop._stop_gaps()` | Todo, command and hook checks before a run may end |
| `config.py` `BudgetConfig`, `LoopGuardConfig`, `max_stop_blocks` | Defaults for all thresholds |
| `agent/subagent.py` `check_contract()` | Verifies a subagent's claimed artifacts exist |

## Related chapters

- [04 The agent loop](/en/core/agent-loop) places the budget check, guard and stop check in each round.
- [07 Context engineering](/en/core/context-engineering) lowers the pressure that causes early stops.
- [10 The run state machine](/en/reliability/run-lifecycle) defines the end statuses as states.
- [12 Cancellation](/en/reliability/cancellation) covers the `cancelled` path.
- [20 Subagents](/en/multi-agent/subagents) covers child budgets and the return contract.
