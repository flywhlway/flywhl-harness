# 20 Subagents and return contracts

*Summary. [Read the full chapter in Chinese](/multi-agent/subagents).*

## The problem

Handing work to another agent raises four questions. Is the split worth it? What does the child get? What comes back? How do you stop it from sinking the parent through context, budget, permissions or cancellation?

The chapter gives three legitimate reasons to split: context isolation (noisy exploration stays in the child, the parent gets a conclusion), parallelism, and a narrower tool set. The costs are real: a summary loses detail, every child pays again for its system prompt and tool definitions, and a vague brief becomes the parent's cleanup. It advises against splitting tightly coupled reasoning, tasks that depend on unwritten context from the user, tiny tasks, and several subtasks editing one file. Whether a split actually helps is a question for evaluation, not taste.

Of the four common patterns (orchestrator-worker, hierarchical, swarm, blackboard), the built-in one is orchestrator-worker through the `task` tool. `subagent_max_depth` (default 2) lets a child delegate once more, and a grandchild has no `task` tool. Swarm-style scheduling is done by a deterministic DAG scheduler, not by a model.

## How it works

**The brief is the child's whole world.** The child starts in a new session with empty history. A usable brief states the goal, scope and boundaries, inputs as references (paths or blob handles), constraints, output schema, a turn budget and a stop condition, including when to report `needs_input`.

**The return contract.** The child must end with one JSON block matching `CONTRACT_SCHEMA`:

- `status`: `ok`, `partial`, `failed` or `needs_input`
- `summary`: at most 2000 characters
- `findings`: each with a `claim`, `evidence` (`path:line` or a handle) and a `confidence` from 0 to 1
- `artifacts`: paths of files produced
- `open_questions`

Free text is rejected because the parent needs conclusion, evidence and artifact location separately, because multi-layer paraphrase distorts, because a schema can be checked without another model call, and because length stays bounded.

**`check_contract()` checks form, not truth.** No JSON block, or a schema violation, is a violation. A declared artifact that does not exist is not: `ok` is downgraded to `partial` and a note goes into `open_questions`, which keeps more information than rejecting. A claim like "config is read from config.toml" passes even if no such file exists. The chapter suggests a `subagent_stop` hook to verify that evidence locations at least resolve.

**Six isolation dimensions in `Harness.run_subagent()`.**

| Dimension | Behavior |
|---|---|
| Context | New session, parent history physically absent; the system prompt is frozen at creation |
| Permissions | Inherits the parent's mode. Session grants ("always allow") are not inherited, so the child may ask again |
| Tools | A subset of the parent's, never more; unknown names are silently dropped; at the depth limit `task` is removed |
| Budget | A child budget capped by the parent's remainder, with `max_tool_calls` at four times `max_turns`; tool calls accumulate upward, turns do not |
| Cancellation | A child token under the parent's; the parent stops, the child stops at its next safe point |
| Blob handles | The child uses the parent's blob scope, so handles pass both ways without copying content |

`TaskTool` is declared `Effect.READ`, so dispatching a child needs no approval, and the child's own writes go through its own permissions. Because it counts as read, several `task` calls in one turn can run concurrently, which is a hazard if they edit the same file. Child runs skip `verify_command`, which belongs to the whole task. A child run is registered as active, so it can be cancelled alone. After a crash it is not recovered by itself: the parent re-spawns it.

**The correction round.** If the contract is invalid and the child run finished as `completed`, the harness starts one more child run in the same child session, listing the violations and asking for only the JSON block. The retry is cheap because the child keeps its own work in context. Both attempts share one child budget, so a first attempt that burns all its turns leaves nothing for the retry. After two violations the contract becomes `failed`, with the raw reply clipped to 500 characters into `findings` at confidence 0.2, so the parent gets clues instead of a bare error. The order matters: a valid contract is accepted even if the run ended on `budget_exhausted`, and a run that ended abnormally with no valid contract gets no retry. A child cancelled on its own comes back as a `failed` contract. If the parent was cancelled too, the cancel propagates up.

**Taint travels up.** If the child read untrusted content, the parent run and the parent session metadata are tainted, because an injection could ride in on `summary` or `findings`. Isolation blocks the raw text, not what the child concluded from it.

**Events go to the UI, not the model.** Child events are stored under the child run and forwarded as nested `tool_update` events on the parent's `task` call, except `message_update` and `tool_update`. The parent's context only gains one tool message: the contract JSON. Details like child session id and usage live in `details`.

**Accounting.** Child tool calls count against the parent's `max_tool_calls`, so several heavy children can exhaust it even if the parent called `task` a few times. Each child run records only its own attempt's usage (`rc.usage_base`), so the shared budget is not counted twice. Summing the `runs` table across parent and children therefore needs care.

## Key code

| Where | What it does |
|---|---|
| `agent/subagent.py` `TaskTool` | The `task` tool: depth check, calls `run_subagent`, wraps the contract as the tool result |
| `agent/subagent.py` `CONTRACT_SCHEMA`, `check_contract()` | The schema and the form check with artifact downgrade |
| `runtime/manager.py` `Harness.run_subagent()` | Session, tools, budget, cancel token, retry loop, taint upload |
| `runtime/manager.py` `_no_verify()` | Strips `verify_command` for child runs |
| `agent/run_context.py` `usage_base`, `run_usage` | Per-attempt usage under a shared budget |
| `prompts.py` `SUBAGENT_PROMPT` | The child's system prompt and the contract it must emit |
| `_utils.py` `extract_json_block()` | Pulls the JSON out of the final reply |
| `examples/06_subagent.py` | Runs the first-violation, then-corrected flow |

## Related chapters

- [21 Swarm and DAG scheduling](/en/multi-agent/swarm-dag) covers parallel children with dependencies.
- [19 Security](/en/governance/security) explains why taint propagates from child to parent.
- [16 Hooks](/en/governance/hooks) covers `subagent_stop` for stronger contract checks.
- [Trajectory, replay and evaluation](/en/multi-agent/trajectory-eval) is how to test whether a split helped.
