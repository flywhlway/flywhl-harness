# Benchmark

The benchmark answers three questions with data:

| | Question | Method |
|---|---|---|
| Q1 | Do the harness mechanisms hold up on real models? | 24 generic scenarios × 5 providers × 3 trials |
| Q2 | What does the default sandbox cost? | 30 small tasks, sandbox on and off, paired, 3 trials each |
| Q3 | Is trajectory replay really deterministic? | Replay every trajectory from Q1 and Q2 and count divergences |

**First results are published.** Plan `2026-11`, commit `333bbb3`, $1.90 by the ledger's estimate. See the [results page](/en/benchmark/results) for every number, the basis for each failure class and the deviations from the plan. The redacted raw data is on the [GitHub Release](https://github.com/flywhlway/flywhl-harness/releases/tag/benchmark-2026-11). The [methodology](/benchmark/methodology) (in Chinese) was fixed before the run.

| Question | Result | How to read it |
|---|---|---|
| Q1 | 5 models, 24 scenarios, 3 trials each. Four passed everything, MiniMax 66/69. All 3 failures are classed as model randomness; 0 harness defects | Not a model ranking |
| Q2, general tasks | 25 tasks: sandbox on 74/75, off 71/75, paired test p = 1.0. Median extra time per trial 0.35s, cost almost identical | No detected difference is not proof of no effect |
| Q2, sandbox-sensitive tasks | 5 tasks: 4 went 0/3 on and 3/3 off, 1 went 3/3 both ways. Cost $0.25 on, $0.06 off | Built to hit the boundary; the share says nothing about daily work |
| Q3 | 555 trajectories replayed, 0 diverged | Recorded runs replay exactly, nothing more |

Two things to read alongside these numbers:

- `env-git-config` passed 3/3 with the sandbox on because the file tool's write to `.git/config` is auto-approved in unattended runs. In 2 of 3 `env-git-hook` runs with the sandbox on, the model used the same route to get around the read-only protection on hooks. This is not fixed and is in the [issue ledger](https://github.com/flywhlway/flywhl-harness/blob/main/docs/quality-audit/issues.md).
- The first attempt crashed partway through because of the benchmark's container setup, not the harness. 4 trials were voided and rerun; the results page records what happened.

What the benchmark will not claim:

- It does not rank models. The scenarios test harness mechanisms, not model ability.
- "Not significant" does not mean "no effect". With 25 general tasks, the exact McNemar test needs at least 6 tasks disagreeing in the same direction to reach p < 0.05.
- It does not say how much success rate the sandbox costs in everyday work. Five of the 30 tasks are built to hit the sandbox boundary, and that share is our choice, so the primary analysis uses the other 25.
