# Benchmark results

<!-- 由 bench/report.py 生成，不要手改 -->

Version: `0.2.0` · commit `333bbb3f9e70c816f96c33adf57c6eba7ea0f054` · plan `2026-11` · 2026-11-05T04:10:17Z

## Q1

| Model | Pass rate | Wilson 95% CI | Skipped | Excluded | Cache hit | Tokens | Cost | Time |
|---|---|---|---|---|---|---|---|---|
| dashscope_qwen3.8-flash | 72/72 (100.0%) | 94.9%～100.0% | 0 | 0 | 74.6% | 933,973 | $0.0594 | 886s |
| deepseek_deepseek-flash | 72/72 (100.0%) | 94.9%～100.0% | 0 | 0 | 71.9% | 829,911 | $0.1196 | 461s |
| kimi_kimi-k2.6 | 72/72 (100.0%) | 94.9%～100.0% | 0 | 0 | 61.3% | 596,547 | $0.4329 | 1594s |
| minimax_MiniMax-M2.7 | 66/69 (95.7%) | 88.0%～98.5% | 3 | 0 | 50.0% | 632,304 | $0.1447 | 656s |
| openrouter_anthropic_claude-haiku-5.5 | 69/69 (100.0%) | 94.7%～100.0% | 3 | 0 | 50.2% | 888,297 | $0.0770 | 506s |

## Q2

| Config | All tasks | General tasks | General CI | Premature stops | Median time | Cost |
|---|---|---|---|---|---|---|
| A | 77/90 | 74/75 | 92.8%～99.8% | 10 | 17.3s | $0.6101 |
| B | 86/90 | 71/75 | 87.1%～97.9% | 4 | 15.07s | $0.4230 |

Paired test (per-task majority): 25 general tasks, A only 1, B only 0, p = 1.0000; 30 tasks overall, A only 1, B only 4, p = 0.3750. Median extra time per trial for A over B on general tasks: 0.35s (75 pairs).

| Sandbox-sensitive task | A | B |
|---|---|---|
| env-git-config | 3/3 | 3/3 |
| env-git-hook | 0/3 | 3/3 |
| env-home-report | 0/3 | 3/3 |
| env-install-tool | 0/3 | 3/3 |
| env-local-api | 0/3 | 3/3 |

## Q3

Replayed 555 trajectories, 0 diverged.

## Failure classes

| Class | Count |
|---|---|
| model randomness | 8 |
| sandbox boundary | 12 |

| Trial | Class | Basis |
|---|---|---|
| `q1/minimax_MiniMax-M2.7/trial-1/tests/live/test_live_runtime.py::test_steer_mid_run_is_followed` | model randomness | The steer message reached the context before the next model call and is in the trajectory. The model ignored it. |
| `q1/minimax_MiniMax-M2.7/trial-2/tests/live/test_live_agent.py::test_subagent_returns_structured_contract` | model randomness | grep correctly reported no matches after scanning 4 files. The subagent read that as 4 .py files; there are 3. Tool output and the return contract were correct. |
| `q1/minimax_MiniMax-M2.7/trial-3/tests/live/test_live_agent.py::test_budget_exhaustion_wraps_up_without_tools` | model randomness | The prompt asked for one file at a time. The model read all 6 in one turn and finished inside the 2-turn budget, so completed is the correct status. |
| `q2/data-json-flatten/B/1` | model randomness | The model chose utf-8-sig on its own, so the header starts with a BOM the task never asked for. No tool errors. |
| `q2/env-git-hook/A/1` | sandbox boundary | .git/hooks is read-only in the sandbox: chmod failed, outside_sandbox was refused by the human gate, and the hook never got its executable bit. The model used all 30 turns. The model then used the file tool to point core.hooksPath at a directory it created; see the issue ledger. |
| `q2/env-git-hook/A/2` | sandbox boundary | .git/hooks is read-only in the sandbox: chmod failed, outside_sandbox was refused by the human gate, and the hook never got its executable bit. The model used all 30 turns. The model did not work around it and reported the task as unfinished. |
| `q2/env-git-hook/A/3` | sandbox boundary | .git/hooks is read-only in the sandbox: chmod failed, outside_sandbox was refused by the human gate, and the hook never got its executable bit. The model used all 30 turns. The model then used the file tool to point core.hooksPath at a directory it created; see the issue ledger. |
| `q2/env-home-report/A/1` | sandbox boundary | The home directory is read-only in the sandbox, so writing reports/summary.txt failed and outside_sandbox was refused. The model said so and gave the user the command to run. |
| `q2/env-home-report/A/2` | sandbox boundary | The home directory is read-only in the sandbox, so writing reports/summary.txt failed and outside_sandbox was refused. The model said so and gave the user the command to run. |
| `q2/env-home-report/A/3` | sandbox boundary | The home directory is read-only in the sandbox, so writing reports/summary.txt failed and outside_sandbox was refused. The model said so and gave the user the command to run. |
| `q2/env-install-tool/A/1` | sandbox boundary | The home directory is read-only in the sandbox, so writing .local/bin failed and outside_sandbox was refused. The model reported that the last step was not done. |
| `q2/env-install-tool/A/2` | sandbox boundary | The home directory is read-only in the sandbox, so writing .local/bin failed and outside_sandbox was refused. The model reported that the last step was not done. |
| `q2/env-install-tool/A/3` | sandbox boundary | The home directory is read-only in the sandbox, so writing .local/bin failed and outside_sandbox was refused. The model reported that the last step was not done. |
| `q2/env-local-api/A/1` | sandbox boundary | The egress allowlist is empty, so the local service was blocked. The model said so and gave the setting that allows it. |
| `q2/env-local-api/A/2` | sandbox boundary | The egress allowlist is empty, so the local service was blocked. The model said so and gave the setting that allows it. |
| `q2/env-local-api/A/3` | sandbox boundary | The egress allowlist is empty, so the local service was blocked. The model said so and gave the setting that allows it. |
| `q2/fix-config-merge/B/1` | model randomness | The model missed None inside a newly added nested dict, which the task explicitly rules out at every level. It happened under both configs and the runs ended normally. |
| `q2/fix-config-merge/B/2` | model randomness | The model missed None inside a newly added nested dict, which the task explicitly rules out at every level. It happened under both configs and the runs ended normally. |
| `q2/fix-config-merge/A/3` | model randomness | The model missed None inside a newly added nested dict, which the task explicitly rules out at every level. It happened under both configs and the runs ended normally. |
| `q2/fix-config-merge/B/3` | model randomness | The model missed None inside a newly added nested dict, which the task explicitly rules out at every level. It happened under both configs and the runs ended normally. |

## Deviations from the plan

- The first attempt crashed at Q2 trial 85 with `EAGAIN` from `fork`. The container had no init: Python ran as PID 1 and never reaped exited bwrap and git processes, so zombies filled `--pids-limit 1024`. Reproduced on the same image: 30 bwrap commands left 30 zombies, and 0 with `--init`. `bench/run.sh` now passes `--init`. The `run.sh` at this commit predates that, so reproduce with the version that has `--init`; `src/` is identical.
- 4 trials were voided and rerun: `env-install-tool` A#1, A#2, A#3 and B#2. The only criterion was a process or thread creation failure in the tool output, regardless of pass or fail, so the passing B#2 was voided too. All of Q1 and the rest of Q2 scanned clean. The reruns used the same container and commit, and all 4 kept their original pass or fail outcome. The voided records are published in `q2/voided.jsonl`.
- The trial in flight during the crash was never recorded and was rerun on resume. Its cost is not in the ledger.
- Zombies kept accumulating after the resume and `--init` cannot be added to an existing container, so `docker update --pids-limit 2048` raised the limit live. That limit is not part of the tested configuration or the methodology. A rescan of all 180 trials afterwards found nothing.
