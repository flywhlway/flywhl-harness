# 21 DAG scheduling and swarms

*Summary. [Read the full chapter in Chinese](/multi-agent/swarm-dag).*

## The problem

A large task often splits into subtasks with dependencies. Then three questions come up on every step: which one runs next, how many run at once, and what happens when one fails. If the model answers them, each answer is a nondeterministic decision that costs a full context of tokens, can't be replayed, and can't enforce a tenant quota the model might forget.

The split here is fixed. The LLM decomposes the goal, writes each subtask's brief and declares dependencies. A deterministic scheduler does validation, readiness, concurrency, retries and failure propagation, and never reads task content. The LLM comes back only at a batch boundary, when the scheduler hands it the results (failures included) so it can plan the remaining work. The graph is never edited mid-run.

## How it works

**Validation first.** `topo_order()` runs Kahn's algorithm before anything is dispatched. It rejects dependencies on unknown task IDs (a typo by the planner is a likely error) and cycles. The cycle error lists every task whose in-degree never reached zero, so with `a <-> b` and `c -> a` it reports `[a, b, c]`: the cycle plus its downstream. A planner asked to repair the graph should know the list is larger than the cycle.

**Event-driven dispatch.** `DagScheduler.run()` calls `topo_order()` only to validate. A task becomes ready the moment its last dependency finishes, tracked with an in-degree table, so downstream work never waits for an unrelated task in the same layer. Ready tasks are ordered by `(priority, id)`, and `priority` only matters among tasks that become ready together.

**Data flow.** A task function receives `{dep_id: value}` from its upstreams. Under the `continue` policy a failed upstream appears as an `UpstreamFailed(id, status, error)` instead of a value, and the downstream decides whether to degrade or give up. In a swarm the value should be the subagent's return contract (conclusion and artifact paths), not its conversation.

**Two-level limits.** `max_concurrency` bounds the whole run and `per_tenant` bounds each tenant. Each task does `async with sem_t, global_sem`: tenant first, global second. A task waiting for its tenant's slot then holds no global slot. With the order reversed, tenant A's third task sits on a global slot while waiting, and tenant B's only task queues behind it with nothing of B's running.

**Failure policies.**

| Policy | Failed task's descendants | Other running tasks | Use when |
|---|---|---|---|
| `fail_fast` | Not dispatched | Cancelled at once | Any failure makes the whole result pointless |
| `skip_downstream` (default) | Marked `skipped`, transitively | Keep running | Subtasks are independent and partial results still count |
| `continue` | Dispatched, with `UpstreamFailed` as input | Keep running | Downstream can report or tolerate a missing input |

The policy is also a cost control. `continue` on a task where one failure invalidates everything pays for results that get thrown away.

**Behaviors worth knowing.**

- Retries happen inside the task while it holds its semaphore slots. The backoff is `min(2.0, 0.1 * 2**attempt)` seconds with no jitter, and the slots stay held during it.
- `timeout` applies to each attempt, and a timeout counts as an ordinary failure that can retry.
- `started_at` is set after both semaphores are acquired, so duration excludes queue time. Cancelled tasks still get `ended_at` in a `finally`.
- The cancel token is awaited together with the running tasks, so a cancel takes effect immediately, not after the next task finishes.
- `retries` on a subagent task re-runs an agent that may have side effects. Use it only for read-only or idempotent work.

**Wiring it to subagents.** There is no built-in swarm wrapper. The chapter gives two sketches. Inside a tool, each task calls `run_subagent()`, so child budgets come out of the parent's, parent cancellation reaches the children, and the contract is enforced with a correction round. Orchestrated outside the harness, each task is its own top-level run: you total `res.usage` yourself, cancel each run yourself, call `check_contract` yourself, and persist scheduler progress yourself.

**Merging conflicting results.** In order of reliability: deterministic checks (tests, schema validation, whether an `evidence` `path:line` exists), voting for discrete answers, an LLM judge for open-ended ones, and escalation to a person through `needs_input` or `open_questions`. Voting with the same model and prompt gives correlated errors, so a majority is not automatically right. `TaskResult` carries `deps`, `attempts` and timestamps so every merged conclusion traces back to its source.

**Critical path.** `critical_path()` computes the longest chain by actual duration. In `examples/07_swarm_dag.py` the printed path is shorter than the wall clock, because only `ok` results are passed in and the failed task's retries count as zero. The path that sets the total time was the failing one.

## Key code

| Where | What it does |
|---|---|
| `runtime/scheduler.py` `TaskSpec`, `TaskResult`, `UpstreamFailed` | Task definition, outcome record, and the marker passed downstream on failure |
| `runtime/scheduler.py` `topo_order()` | Kahn sort that rejects unknown dependencies and cycles |
| `runtime/scheduler.py` `DagScheduler.run()` | In-degree dispatch, two-level semaphores, retries, failure policies, cancellation |
| `runtime/scheduler.py` `critical_path()` | Longest path by measured duration |
| `runtime/manager.py` `run_subagent()` | The subagent entry point a swarm task calls |
| `examples/07_swarm_dag.py` | Eight-task migration with simulated work and one forced failure |

## Related chapters

- [20 Subagents](/en/multi-agent/subagents) defines the return contract each task produces.
- [22 Trajectories, replay and evaluation](/en/multi-agent/trajectory-eval) answers whether a swarm beats a single agent.
- [24 Long-running harnesses](/en/advanced/long-running) covers leases and queues, which a multi-process scheduler needs.
