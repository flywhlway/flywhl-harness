---
layout: home

hero:
  name: "Agent = Model + Harness"
  text: "An Agent Harness, written by hand"
  tagline: The model does the thinking. The harness does everything else, from loop and tools to context, state, lifecycle, policy, execution and measurement. These docs walk through each mechanism in a runnable Python implementation.
  image:
    src: /favicon.svg
    alt: flywhl
  actions:
    - theme: brand
      text: Quick start
      link: /en/guide/quickstart
    - theme: alt
      text: What is a harness
      link: /en/guide/what-is-harness
    - theme: alt
      text: Architecture
      link: /en/guide/architecture

features:
  - icon: ⟳
    title: Loop and tools
    details: An async-generator main loop, the tool_call and tool_result pairing invariant, side-effect levels, concurrent reads with results returned in order.
    link: /en/core/agent-loop
  - icon: ◐
    title: Context engineering
    details: Context usage estimates, clearing stale results, compaction cut points, plan recitation, and why prompt caching demands append-only history.
    link: /en/core/context-engineering
  - icon: ⛁
    title: Runs that survive crashes
    details: A four-layer state model, a compare-and-swap state machine, an intent-then-result journal, and exactly-once side effects after kill -9.
    link: /en/reliability/checkpoint-recovery
  - icon: ⏹
    title: Cancellation, retries, idempotency
    details: Cooperative cancellation and the unknown outcome, error classification, jittered backoff, circuit breaking, idempotency keys and reconciliation.
    link: /en/reliability/cancellation
  - icon: ⚖
    title: Policy and security
    details: Hooks, four permission modes, bash analysis, taint tracking, durable human approval, a default-on OS sandbox and supply-chain checks.
    link: /en/governance/workspace-sandbox
  - icon: ⧉
    title: Multi-agent and measurement
    details: Subagent return contracts, DAG scheduling, trajectories and deterministic replay, evaluation with confidence intervals, OTel GenAI spans.
    link: /en/multi-agent/subagents
---

<div style="max-width: 960px; margin: 48px auto 0; padding: 0 24px;">

## How to read these docs

The full tutorial is written in Chinese: 30 chapters of principles, design trade-offs and source walkthroughs. This English site has the
[quick start](/en/guide/quickstart) and [architecture overview](/en/guide/architecture) in full, and a summary of every other chapter: the
problem, the mechanism, and where the key code lives. Each summary links to the Chinese chapter.

| If you want to | Start here |
|---|---|
| Run it | [Quick start](/en/guide/quickstart): `pipx install flywhl-harness`, then a scripted demo with no API key |
| See the whole shape | [What is a harness](/en/guide/what-is-harness), then the [architecture overview](/en/guide/architecture) |
| Know what the sandbox does and doesn't do | [Workspace and sandbox](/en/governance/workspace-sandbox), then [security](/en/governance/security) |
| Follow the crash-recovery story | [State and persistence](/en/reliability/state-persistence) through [idempotency](/en/reliability/idempotency) |
| Look up a term | [Glossary](/en/reference/glossary), with the Chinese term for each entry |

</div>
