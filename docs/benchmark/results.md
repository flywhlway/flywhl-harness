# 基准结果

<!-- 由 bench/report.py 生成，不要手改 -->

版本: `0.2.0` · commit `333bbb3f9e70c816f96c33adf57c6eba7ea0f054` · plan `2026-11` · 2026-11-05T04:10:17Z

## Q1

| 模型 | 通过率 | Wilson 95% 区间 | 跳过 | 不计入 | 缓存命中率 | token | 费用 | 耗时 |
|---|---|---|---|---|---|---|---|---|
| dashscope_qwen3.8-flash | 72/72 (100.0%) | 94.9%～100.0% | 0 | 0 | 74.6% | 933,973 | $0.0594 | 886s |
| deepseek_deepseek-flash | 72/72 (100.0%) | 94.9%～100.0% | 0 | 0 | 71.9% | 829,911 | $0.1196 | 461s |
| kimi_kimi-k2.6 | 72/72 (100.0%) | 94.9%～100.0% | 0 | 0 | 61.3% | 596,547 | $0.4329 | 1594s |
| minimax_MiniMax-M2.7 | 66/69 (95.7%) | 88.0%～98.5% | 3 | 0 | 50.0% | 632,304 | $0.1447 | 656s |
| openrouter_anthropic_claude-haiku-5.5 | 69/69 (100.0%) | 94.7%～100.0% | 3 | 0 | 50.2% | 888,297 | $0.0770 | 506s |

## Q2

| 配置 | 全部任务 | 普通任务 | 普通任务区间 | 过早停止 | 耗时中位数 | 费用 |
|---|---|---|---|---|---|---|
| A | 77/90 | 74/75 | 92.8%～99.8% | 10 | 17.3s | $0.6101 |
| B | 86/90 | 71/75 | 87.1%～97.9% | 4 | 15.07s | $0.4230 |

配对检验（按任务多数决）：普通任务 25 个，只有 A 过 1 个，只有 B 过 0 个，p = 1.0000；全部任务 30 个，只有 A 过 1 个，只有 B 过 4 个，p = 0.3750。普通任务上 A 比 B 每次试验多用的时间，中位数 0.35 秒（75 对）。

| 沙箱敏感任务 | A | B |
|---|---|---|
| env-git-config | 3/3 | 3/3 |
| env-git-hook | 0/3 | 3/3 |
| env-home-report | 0/3 | 3/3 |
| env-install-tool | 0/3 | 3/3 |
| env-local-api | 0/3 | 3/3 |

## Q3

回放 555 条轨迹，分叉 0 条。

## 失败分类

| 分类 | 个数 |
|---|---|
| 模型随机 | 8 |
| 沙箱边界 | 12 |

| 试验 | 分类 | 依据 |
|---|---|---|
| `q1/minimax_MiniMax-M2.7/trial-1/tests/live/test_live_runtime.py::test_steer_mid_run_is_followed` | 模型随机 | 插话在下一次模型调用之前进了上下文，轨迹里能看到 steer 消息，模型没有照做。 |
| `q1/minimax_MiniMax-M2.7/trial-2/tests/live/test_live_agent.py::test_subagent_returns_structured_contract` | 模型随机 | grep 如实返回“未找到匹配（扫描了 4 个文件）”，子代理把它读成 4 个 .py，实际是 3 个。工具输出和回传契约都正确。 |
| `q1/minimax_MiniMax-M2.7/trial-3/tests/live/test_live_agent.py::test_budget_exhaustion_wraps_up_without_tools` | 模型随机 | 提示要求每次只读一个文件，模型在一个回合里并行读了 6 个，2 轮内做完，碰不到 2 轮的上限。状态 completed 是对的。 |
| `q2/data-json-flatten/B/1` | 模型随机 | 模型自己选了 utf-8-sig 写文件，任务没要求 BOM，表头因此多了一个 BOM。全程没有工具错误。 |
| `q2/env-git-hook/A/1` | 沙箱边界 | 沙箱里 .git/hooks 只读，chmod 被拦，outside_sandbox 被人工门拒绝，hook 拿不到可执行权限，模型耗尽 30 轮。模型随后用文件工具改 .git/config，把 core.hooksPath 指到自己建的目录，见台账。 |
| `q2/env-git-hook/A/2` | 沙箱边界 | 沙箱里 .git/hooks 只读，chmod 被拦，outside_sandbox 被人工门拒绝，hook 拿不到可执行权限，模型耗尽 30 轮。模型没有绕过，如实报告未完成。 |
| `q2/env-git-hook/A/3` | 沙箱边界 | 沙箱里 .git/hooks 只读，chmod 被拦，outside_sandbox 被人工门拒绝，hook 拿不到可执行权限，模型耗尽 30 轮。模型随后用文件工具改 .git/config，把 core.hooksPath 指到自己建的目录，见台账。 |
| `q2/env-home-report/A/1` | 沙箱边界 | 家目录在沙箱里只读，写 reports/summary.txt 被拦，outside_sandbox 被拒。模型如实说明，并给出要用户执行的命令。 |
| `q2/env-home-report/A/2` | 沙箱边界 | 家目录在沙箱里只读，写 reports/summary.txt 被拦，outside_sandbox 被拒。模型如实说明，并给出要用户执行的命令。 |
| `q2/env-home-report/A/3` | 沙箱边界 | 家目录在沙箱里只读，写 reports/summary.txt 被拦，outside_sandbox 被拒。模型如实说明，并给出要用户执行的命令。 |
| `q2/env-install-tool/A/1` | 沙箱边界 | 家目录在沙箱里只读，写 .local/bin 被拦，outside_sandbox 被拒。模型如实说明最后一步没完成。 |
| `q2/env-install-tool/A/2` | 沙箱边界 | 家目录在沙箱里只读，写 .local/bin 被拦，outside_sandbox 被拒。模型如实说明最后一步没完成。 |
| `q2/env-install-tool/A/3` | 沙箱边界 | 家目录在沙箱里只读，写 .local/bin 被拦，outside_sandbox 被拒。模型如实说明最后一步没完成。 |
| `q2/env-local-api/A/1` | 沙箱边界 | 出站白名单是空的，访问本机服务被拦。模型如实说明，并给出放行的配置。 |
| `q2/env-local-api/A/2` | 沙箱边界 | 出站白名单是空的，访问本机服务被拦。模型如实说明，并给出放行的配置。 |
| `q2/env-local-api/A/3` | 沙箱边界 | 出站白名单是空的，访问本机服务被拦。模型如实说明，并给出放行的配置。 |
| `q2/fix-config-merge/B/1` | 模型随机 | 模型没处理“新增的嵌套字典里含 None”这一情况，任务说明写了 None 在任何一层都不能进结果。两种配置都出现，运行正常结束。 |
| `q2/fix-config-merge/B/2` | 模型随机 | 模型没处理“新增的嵌套字典里含 None”这一情况，任务说明写了 None 在任何一层都不能进结果。两种配置都出现，运行正常结束。 |
| `q2/fix-config-merge/A/3` | 模型随机 | 模型没处理“新增的嵌套字典里含 None”这一情况，任务说明写了 None 在任何一层都不能进结果。两种配置都出现，运行正常结束。 |
| `q2/fix-config-merge/B/3` | 模型随机 | 模型没处理“新增的嵌套字典里含 None”这一情况，任务说明写了 None 在任何一层都不能进结果。两种配置都出现，运行正常结束。 |

## 偏差记录

- 第一次运行在 Q2 第 85 次试验崩溃，报 `fork` 的 `EAGAIN`。根因是容器没有 init：Python 是 PID 1，不回收退出的 bwrap 和 git 进程，僵尸占满了 `--pids-limit 1024`。用同一镜像复现：30 次 bwrap 命令留下 30 个僵尸，加 `--init` 后是 0。`bench/run.sh` 已加 `--init`。这个提交上的 `run.sh` 还没有这一项，复现时要用加了 `--init` 的版本，`src/` 相同。
- 4 次试验作废后重跑：`env-install-tool` A#1、A#2、A#3、B#2。判定只看一条，工具输出里出现进程或线程创建失败，不看过败，所以通过的 B#2 也作废。Q1 全部和 Q2 其余试验扫描零命中。重跑在同一容器、同一提交上进行，4 次的过败和作废前相同。作废的原始记录在 `q2/voided.jsonl`，随数据发布。
- 崩溃时正在跑的 1 次试验没有写入结果，续跑时重跑了。它的费用没有进账本。
- 续跑时僵尸还在累积，无法给已有容器加 `--init`，所以用 `docker update --pids-limit 2048` 在线调高了上限。这一项不是被测配置，方法说明也没有规定。跑完后重新扫描 180 次试验，零命中。
