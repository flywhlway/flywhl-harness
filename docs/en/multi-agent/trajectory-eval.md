# 22 Trajectories, replay and evaluation

*Summary. [Read the full chapter in Chinese](/multi-agent/trajectory-eval).*

## The problem

No two runs of an agent are identical. Three things follow. You need to reproduce a run. After a harness change you need to find the step where behavior started to differ. And "the new version is better" needs a statistical argument that survives a sample of a few dozen cases.

## How it works

**What a trajectory keeps.** Four kinds of data: the inputs (prompt, frozen system prompt, tool set, model); every nondeterministic result (assistant messages, tool results, usage); control-plane events (status changes, approvals, cancels, compaction, retries); and a context fingerprint per model call. The first two make replay possible. The last two make a divergence explainable. `export_trajectory()` bundles them into a `flywhl-trajectory/1` document with the run, session settings, messages, compactions, the tool-call journal, events (minus `message_update` deltas), fingerprints, child run trajectories and OTel-style spans.

**Context fingerprints.** `context_fingerprint()` hashes the `ModelRequest` that is actually about to be sent, after tool-result clearing: the system prompt, each tool's name plus hashes of its description and parameter schema, and each message's role, text, tool-call names and `tool_call_id`. So a one-sentence change to a tool description changes the fingerprint, as do changes to compaction, clearing or truncation rules. Temperature, `max_tokens`, the model name, thinking blocks and images are not hashed. The fingerprint is recorded on the `message_start` event, and export keeps only those of calls that produced a valid assistant message (no errors, aborts or compaction calls), so they line up one to one with the replayed output.

**Deterministic replay.** `replay_run()` runs the real `Harness` with the real loop and execution pipeline, and swaps only the two nondeterministic sources for recordings. It uses an in-memory store, a copy of the workspace in a temp directory, the frozen system prompt, auto-approval and no snapshots.

- `ReplayProvider` returns recorded assistant messages in order and compares each request's fingerprint with the recording. It keeps only the first mismatch as `divergence`, because everything after is mismatched too. It keeps returning the recorded output even after a mismatch, so tool sequence and final text can still match. The fingerprint is the only signal that the harness changed. If the new code makes more model calls than were recorded, it raises `IndexError`, which is itself a regression signal. Compaction requests get a deterministic summary and consume no recording.
- `RecordedTool` matches results by `call_id` and returns them with `replayed: True`. External side effects are not executed again.

**Counterfactual replay.** With `fork_at=N`, `HybridProvider` uses recordings for the first N model calls and a live model after that. Recorded tool results cover the calls that came from the recording. After the fork, new call IDs have no recording, so the real tool runs, except that `EXTERNAL`-effect tools are dry-run. This is shadow traffic done offline.

**Replay limits.**

- The workspace copy is the current one, so state before the fork point is not restored. Shadow-git snapshots could restore it.
- Forked runs use `bypass` mode, so `WRITE`-level commands really run, on the copy. Run fork evaluations in a sandbox.
- Subagent internals are not replayed (`include_children=False`).
- Verify commands and wall-clock budgets can behave differently in the copy and change the call count.

**Judging success by artifact.** `EvalCase.check` receives the workspace and the `RunResult`, and looks at files and test output, not at what the model claimed. `run_eval()` gives every case and every trial a fresh temp workspace and a fresh harness, calls `start()` so MCP tools are loaded, and records pass/fail, status, turns, tokens and cost. `EvalReport.premature_stop_rate` is the share of runs that ended `completed` but failed the check. A budget or error failure is at least honest; this one is not.

**Small-sample statistics.**

- `wilson_interval()` gives a confidence interval that stays inside [0, 1] and has real width at 12/12 (about 0.76 to 1.00). The normal approximation gives zero width there.
- `mcnemar_exact()` compares two versions on the same cases using only the cases where they disagree, with an exact two-sided binomial test. A one-sided split of n disagreements gives p = 2/2^n, so at least 6 lopsided cases are needed to pass 0.05.
- `trials` repeats are not independent samples. Hard cases stay hard, so treating them as independent makes the interval look too narrow.

**Evaluators fail too.** An existence-only check passes an empty file. Reading the model's own claim of completion defeats the point. An agent can edit the tests it is graded by. LLM judges prefer the first position, longer answers and their own family's output. The defense is a set of known-bad outputs the evaluator must fail and a set of known-good outputs it must pass.

## Key code

| Where | What it does |
|---|---|
| `observe/trace.py` `export_trajectory()` | Builds the trajectory document, including child runs and spans |
| `llm/replay.py` `context_fingerprint()` | Hashes the request the model actually saw |
| `llm/replay.py` `ReplayProvider` | Serves recorded outputs and records the first divergence |
| `observe/replay.py` `replay_run()` | Assembles replay or fork from a recorded run |
| `observe/replay.py` `RecordedTool`, `HybridProvider` | Recorded tool results with external dry-run; the record-then-live switch |
| `observe/evals.py` `run_eval()`, `EvalCase` | Artifact-checked evaluation with fresh workspaces |
| `observe/evals.py` `wilson_interval()`, `mcnemar_exact()` | Interval and paired test |
| `cli.py` `flywhl trace`, `flywhl replay --fork-at N` | Command line entry points |
| `examples/08_replay_eval.py` | Records a run, replays it twice, then compares two eval versions |

## Related chapters

- [20 Subagents](/en/multi-agent/subagents) explains why child runs are nested in the export.
- [23 Observability and cost](/en/multi-agent/observability-cost) covers the spans in the trajectory.
- [08 Termination and budgets](/en/core/termination-budget) holds the stop checks that premature stop rate measures.
