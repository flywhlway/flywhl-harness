# 01 What is a harness

*Summary. [Read the full chapter in Chinese](/guide/what-is-harness).*

## The problem

The model does one thing: it reads the context and decides what to say or call next. Everything else is the harness: building the context, running the tools, feeding results back, deciding whether to continue, handling errors, and carrying on after a crash. The usual tutorial loop shows the gap. It is about fifteen lines (call the model, append the reply, run each tool call, append the results, stop when there are no tool calls), and it fails on the first hard question:

| Question | What the fifteen lines do | What the harness adds |
|---|---|---|
| The model calls a tool that doesn't exist, or passes arguments that break the schema | `KeyError` or `TypeError`, the loop dies | Validation, and an error message the model can act on |
| A tool returns 100,000 lines of log | The context overflows | Truncation, blob handles, compaction |
| The model calls the same tool forever | Budget burns down | Turn, token and cost budgets; loop detection |
| The user clicks stop during an external notification | Nobody knows whether it was sent | Cooperative cancellation and reconciliation of unknown outcomes |
| The process is killed mid-tool | Progress is lost, and a rerun sends the notification twice | Checkpoints, a journal, idempotency keys |
| The model wants to run `rm -rf ~` | It runs | Permissions, approval, hooks, sandbox |
| The model says "done" but never ran the tests | Believed | Stop hooks and artifact checks |
| Success rate drops after a release | No way to tell what changed | Trajectories, replay, evals |

The chapter frames this as `Agent = Model + Harness`: the same model behaves very differently inside different harnesses.

## How it works

The chapter splits a harness into eight dimensions, each mapped to a group of modules:

| Dimension | Question it answers | Code |
|---|---|---|
| Loop | What happens in one round, and when does it continue? | `agent/loop.py` |
| Tools | How does the model act on the world safely? | `tools/`, `agent/executor.py` |
| Context | What goes in a finite window? | `agent/context.py`, `agent/compaction.py` |
| State | What is the source of truth? | `runtime/store.py`, `runtime/session.py` |
| Lifecycle | Pause, resume, cancel, retry, crash | `runtime/state.py`, `cancel.py`, `runtime/manager.py` |
| Policy | What may run, what needs a person, what never runs? | `policy/`, `agent/budget.py` |
| Execution environment | Where do tools run? | `runtime/workspace.py`, `runtime/dispatch.py` |
| Measurement | Did it work, and why? | `observe/` |

Two layers cut across these. The capability-compensation layer covers what current models do badly (forced planning, format-repair retries, aggressive summaries) and should shrink as models improve. The governance layer (permissions, audit, idempotency, persistence, quotas) does not depend on how smart the model is. The chapter's argument is that the first layer should be switchable and removable, which [28 Evolving harness](/en/advanced/evolving-harness) takes up.

**Harness versus framework.** A harness is a runtime that drives a model through a task. A framework is a set of programming abstractions (graphs, chains, memory interfaces) you assemble a harness from. A workflow fixes the path in advance and uses the LLM as a node. The chapter says to prefer a workflow when steps can be written down beforehand, errors are costly and hard to correct, or latency and cost are tight. An agent fits when the task is multi-step with an unpredictable path, the result is worth the cost, the model can do the task, and errors can be detected and undone. If any of the four fails, use something simpler.

**Loop patterns.** ReAct is the default loop itself. Plan-and-Execute maps to the `todo_write` external plan with recitation, or to the DAG scheduler. Reflexion maps to Stop checks that block a false "done" and feed the gap back into the next round. The chapter recommends deterministic verifiers (tests, schemas, resolvable citations) over asking the model to grade itself.

**Brain, hands, session.** The session is an append-only event log outside the context window and is the only source of truth; the context is a projection of it. The brain (model plus loop) holds no critical state, so a crashed process can be woken from the session. The hands (tools, sandbox) sit behind one call interface and can be a local process, a remote sandbox or a user's laptop. In this code the session is `runtime/store.py` plus `runtime/session.py`, the brain is `agent/loop.py`, the hands are `tools/`, and `Harness.recover()` is the wake-up step.

## Key code

| Location | What it does |
|---|---|
| `agent/loop.py` | The brain: the phased main loop |
| `tools/`, `agent/executor.py` | The hands: tool definitions and batch execution |
| `runtime/store.py`, `runtime/session.py` | The session: persisted source of truth |
| `runtime/manager.py` `Harness.recover()` | Resumes unfinished runs after a restart |
| `policy/` | Hooks, permissions, approvals, command analysis |
| `observe/` | Trace, replay, evals, export |

The project describes itself as minimal but not a toy: no agent framework underneath, and tests cover each mechanism, including a crash-recovery test that really kills the process. It lists what it leaves out: no microVM beyond the OS sandbox, SQLite on one machine, and multi-worker support limited to a lease and takeover demo.

## Related chapters

- [02 Quick start](/en/guide/quickstart) runs the project.
- [03 Architecture overview](/en/guide/architecture) walks one request through every layer.
- [04 The agent loop](/en/core/agent-loop) is the first dimension in detail.
