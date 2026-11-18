# 0.3.0 发布门禁记录

日期：2026-11-18。候选代码：`104b2b1`，内容是 `main`（`c332fd9`）加版本号。之后合入了只改测试的 [#52](https://github.com/flywhlway/flywhl-harness/pull/52)，其余改动都是文档、`bench/regression.toml` 的编号和覆盖率基线，`src/`、`scripts/`、`pyproject.toml`、`uv.lock`、工作流和候选相同。

和 `v0.2.1` 相比，`src/` 包含之后合入 `main` 的全部改动，内容见 [CHANGELOG](../../../CHANGELOG.md) 的 0.3.0 一节。

## 结果

| 检查 | 环境 | 结果 |
|---|---|---|
| 离线套件 | macOS 主检出，Python 3.11.15、3.12.13、3.13.12 | 各 2231 passed，1 skipped |
| 系统沙箱用例 | 同上 | 各 37 passed，7 skipped |
| 审计不变量 | macOS | 13 passed |
| 离线套件 | 一次性 Linux 容器，Python 3.11、3.12.16、3.13，bubblewrap | 各 2228 passed，4 skipped |
| 系统沙箱用例 | 同上 | 各 32 passed，12 skipped |
| 审计不变量 | 同上 | 12 passed，1 skipped |
| kill -9 恢复示例 | 同上 | 通过，外部通知恰好 1 次 |
| Anthropic SDK 离线探针 | 同上，anthropic 2.8.0 | 通过 |
| sdist 解包后测试 | 一次性 Linux 容器，解包到家目录 | 候选上收集失败，[#52](https://github.com/flywhlway/flywhl-harness/pull/52) 修复后 2108 passed，14 skipped；系统沙箱用例 32 passed，16 skipped |
| 沙箱验收（`--internet`） | 一次性 Linux 容器 | 23/23 |
| 覆盖率 | 同上 | 整体 89.95%，全部关键模块高于基线，见下文 |
| 回答语言跟随用户 | 一次性 Linux 容器，cursor-llm-proxy 上的 grok-4.7-medium | 英文提问的回答没有汉字，中文提问回答中文 |
| 基准彩排 | 一次性 Docker 容器，脚本化模型 | 符合预期：60 条试验，普通任务两种配置都过，5 个沙箱敏感任务只有关沙箱才过；回放 60 条分叉 0；脱敏命中 0 |

TODO：macOS 打包与冒烟、沙箱验收、pip-audit、npm audit、actionlint、密钥扫描、live、基准回归、CI。

## 覆盖率

在一次性 Linux 容器里算，和 CI 的 `coverage-audit` 作业同一种环境，分支覆盖一起计入。

| 模块 | 基线 | 候选 |
|---|---:|---:|
| 整体 | 83.28 | 89.95 |
| `policy/permissions.py` | 98.60 | 100.00 |
| `policy/approvals.py` | 82.56 | 100.00 |
| `policy/hooks.py` | 93.91 | 100.00 |
| `tools/sandbox.py` | 89.16 | 99.50 |
| `tools/egress.py` | 94.56 | 99.62 |
| `tools/confine.py` | 92.99 | 100.00 |
| `tools/shell.py` | 86.26 | 97.74 |
| `tools/fs.py` | 77.44 | 99.40 |
| `agent/executor.py` | 85.68 | 99.47 |
| `runtime/manager.py` | 79.12 | 99.29 |
| `net/client.py` | 95.00 | 96.58 |
| `net/exfil.py` | 95.00 | 96.77 |
| `tools/web.py` | 87.00 | 90.64 |
| `runtime/capabilities.py` | 99.00 | 100.00 |

## 门禁查出并修掉的问题

| 问题 | 不修的后果 | 处理 |
|---|---|---|
| `tests/test_web_eval.py` 在模块顶层导入 `bench.web_eval`，而 sdist 不带 `bench/` | 下游打包者解包后跑测试，收集阶段就中止，一个用例都跑不了 | [#52](https://github.com/flywhlway/flywhl-harness/pull/52)，和 `tests/test_bench.py` 一样 `importorskip` |

## 没有闭环的事

- **放行判定没有改。** [release-readiness.md](../release-readiness.md) 仍是首轮的判定。PR #4 的 `src/` 改动还没有逐行审，Dependabot 安全更新和私密漏洞报告也没开，改判留到这些做完之后。
