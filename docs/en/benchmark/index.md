# Benchmark

The benchmark answers three questions with data:

| | Question | Method |
|---|---|---|
| Q1 | Do the harness mechanisms hold up on real models? | 24 generic scenarios × 5 providers × 3 trials |
| Q2 | What does the default sandbox cost? | 30 small tasks, sandbox on and off, paired, 3 trials each |
| Q3 | Is trajectory replay really deterministic? | Replay every trajectory from Q1 and Q2 and count divergences |

**There are no results yet.** The [methodology](/benchmark/methodology) (in Chinese) is written before anything runs, so that metrics cannot be picked after seeing the outcome. Raw data will be attached to a GitHub Release, with pass rates, the paired test, divergence counts and one command to reproduce.

What the benchmark will not claim:

- It does not rank models. The scenarios test harness mechanisms, not model ability.
- "Not significant" does not mean "no effect". With 25 general tasks, the exact McNemar test needs at least 6 tasks disagreeing in the same direction to reach p < 0.05.
- It does not say how much success rate the sandbox costs in everyday work. Five of the 30 tasks are built to hit the sandbox boundary, and that share is our choice, so the primary analysis uses the other 25.
