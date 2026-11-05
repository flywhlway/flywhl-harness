# 0.2.0 发布门禁记录

日期：2026-11-05。候选代码：`5a5d67c`。live 和语言检查跑在 `00abba7` 上，两个提交的 `src/`、`tests/`、`scripts/`、`pyproject.toml`、`uv.lock` 没有差异，之间只改了工作流。

## 结果

| 检查 | 环境 | 结果 |
|---|---|---|
| 离线套件 | macOS，Python 3.11.15、3.12.13、3.13.12 | 各 726 passed |
| 系统沙箱用例 | macOS，三个 Python 版本 | 各 27 passed，6 个 Linux 专用的跳过 |
| 离线套件 | 一次性 Linux 容器，Python 3.12.15，bubblewrap 0.12.0 | 723 passed，3 skipped |
| 系统沙箱用例 | 同上 | 21 passed，12 个 macOS 专用的跳过 |
| 审计不变量 | macOS / Linux | 13 passed / 12 passed，1 skipped |
| sdist 解包后测试 | 一次性 Linux 容器 | 722 passed，4 skipped；系统沙箱用例 21 passed，12 skipped |
| wheel 干净安装冒烟 | macOS 上 3.11～3.13、Linux 上 3.12 | 全部通过 |
| 沙箱验收（`--internet`） | macOS / Linux | 23/23 / 23/23 |
| `twine check --strict` | twine 7.0.0 | wheel、sdist 通过 |
| `pip-audit`（运行依赖，含 anthropic 可选依赖） | 2.10.1 | 没有已知漏洞 |
| 工作流静态检查 | actionlint | 没有告警 |
| 33 个 live 场景 | 一次性 Linux 容器，真实 bubblewrap，deepseek-flash | 33 passed，282,938 token，0.0389 美元，176 秒 |
| 回答语言跟随用户 | 同上 | 英文提问的回答没有汉字，中文提问回答中文 |

Python 3.11、3.13 和 Linux 的完整矩阵，以 PR 上的 CI 结果为准；上表是本机和容器里的复现。

## 门禁查出并修掉的问题

| 问题 | 不修的后果 | 处理 |
|---|---|---|
| `twine@6.2.0` 不认 hatchling 1.33 生成的 Metadata 2.5 | 发布流水线卡在元数据检查 | 换 twine 7.0.0，和 `gh-action-pypi-publish` v1.14.3 自带的版本一致 |
| `test_web_logo_matches_docs_site` 依赖不在 sdist 里的 `docs/public/favicon.svg` | 下游打包者解包后跑测试失败 | 文件不存在时跳过，所以 sdist 比仓库多 1 个跳过 |
| `astral-sh/setup-uv` 用可变标签 | 上游标签被改时，影响会发到 PyPI 的构建产物 | 钉到 v6.8.0 的提交 SHA |

第一次跑 Linux 离线时有 6 条失败，是这次的运行方式不对：把 `FLYWHL_TEST_REAL_SANDBOX=1` 用到了全部用例，这个变量只给 live 用。按 CI 的方式重跑后没有失败。

## 已评估的例外：站点依赖

`docs/` 的 `npm audit` 有 6 条：vite（high）、esbuild、vitepress、vitepress-plugin-mermaid（moderate）、mermaid、katex（low）。不影响发布：

- 这些包只用来构建站点，不进 wheel 和 sdist。
- vite、esbuild 的问题在本地 dev server 上，线上是 GitHub Pages 的静态文件。vitepress 1.6.4 没有修复版本。
- katex 经 mermaid 引入，页面内容是仓库自己的文档，没有外部输入。修复要 mermaid 12，`vitepress-plugin-mermaid` 的 peer 依赖是 `10 || 11`，不用 `--force` 硬升。

VitePress 或插件出修复版本后再升。

## 没有闭环的事

- `test_steer_mid_run_is_followed`、`test_pause_and_resume_at_safe_point` 在 11-04 失败过一次，原因没有定位。之后 4 次完整运行都没有复现，诊断信息已经加上，详见 [tests/live/README.md](../../../tests/live/README.md)。
- live 只在 deepseek-flash 上跑了完整场景，其他供应商只覆盖各自的多步任务和续聊用例。
- 这份记录不代表已经发布。PyPI、TestPyPI 的发布者配置、GitHub environment 和标签都还没做。

## 并入基准试跑的修复之后复核（2026-11-05）

候选代码更新为 `d338d16`。`5a5d67c` 之后 `src/` 的改动有回放、压缩退回、错误分类、OpenRouter 上 Claude 的缓存、命令环境变量过滤和数据目录的 `.gitignore`，细节见 [CHANGELOG](../../../CHANGELOG.md)。`uv.lock`、`pyproject.toml`、`scripts/`、工作流、`policy/`、`tools/sandbox.py`、`tools/egress.py`、`tools/confine.py` 没有改动，所以 `pip-audit`、工作流静态检查和沙箱验收没有重跑，沿用上面的结果。

| 检查 | 环境 | 结果 |
|---|---|---|
| 离线套件 | macOS，Python 3.11.15、3.12.13、3.13.12 | 各 805 passed |
| 系统沙箱用例 | macOS，三个 Python 版本 | 各 26 passed，7 skipped |
| 离线套件 | 一次性 Linux 容器，Python 3.12，bubblewrap 0.12.0 | 801 passed，4 skipped |
| 系统沙箱用例 | 同上 | 20 passed，13 skipped |
| 审计不变量 | macOS / Linux | 13 passed / 12 passed，1 skipped |
| sdist 解包后测试 | 一次性 Linux 容器 | 745 passed，5 skipped；系统沙箱用例 20 passed，14 skipped |
| wheel 干净安装冒烟 | macOS 上 3.11～3.13 | 全部通过 |
| `twine check --strict` | twine 7.0.0 | wheel、sdist 通过 |
| 33 个 live 场景 | 一次性 Linux 容器，真实 bubblewrap，deepseek-flash | 33 passed，275,212 token，0.0298 美元，183 秒 |

多出来的系统沙箱 skip 来自 `tests/test_bench.py`：sdist 不带 `bench/`，这个文件整个跳过。

第一次跑 sdist 解包测试时解压到了 `/tmp`，外层 bubblewrap 用不了，退回只隔离环境变量的模式，`test_command_hook_is_killed_on_timeout` 失败 1 条。解压到家目录、走正常的沙箱之后全部通过。这条用例用 `os.kill(pid, 0)` 判断子进程还在不在，推测是容器里没有 init 回收僵尸进程，没有单独验证。

Python 3.11、3.13 的 Linux 矩阵仍以 PR 上的 CI 结果为准。

## 权限修复合入之后复核（2026-11-05）

候选代码更新为 `dd4331a`，`src/` 和[回归记录](bench-regress-2026-11-05.md)用的 `c86ae89` 相同。这次修复改了 `policy/`、`tools/sandbox.py`，上一节沿用沙箱验收和 `pip-audit` 结果的前提不再成立，所以这两项一起重跑。`d338d16` 之后工作流、`uv.lock`、`pyproject.toml`、`scripts/` 没有改动，工作流静态检查沿用原结果。

| 检查 | 环境 | 结果 |
|---|---|---|
| 离线套件 | macOS，Python 3.11.15、3.12.13、3.13.12 | 各 851 passed |
| 系统沙箱用例 | macOS，三个 Python 版本 | 各 26 passed，7 skipped |
| 离线套件 | 一次性 Linux 容器，Python 3.12，bubblewrap 0.12.0 | 847 passed，4 skipped |
| 系统沙箱用例 | 同上 | 20 passed，13 skipped |
| 审计不变量 | macOS / Linux | 13 passed / 12 passed，1 skipped |
| sdist 解包后测试 | 一次性 Linux 容器 | 780 passed，5 skipped；系统沙箱用例 20 passed，14 skipped |
| wheel 干净安装冒烟 | macOS 上 3.11～3.13、Linux 上 3.12 | 全部通过 |
| 沙箱验收（`--internet`） | macOS / Linux | 23/23 / 23/23 |
| `twine check --strict` | twine 7.0.0 | wheel、sdist 通过 |
| `pip-audit`（运行依赖，含 anthropic 可选依赖） | 2.10.1 | 没有已知漏洞 |
| 33 个 live 场景 | 一次性 Linux 容器，真实 bubblewrap，deepseek-flash | 33 passed，275,592 token，0.0291 美元，171 秒 |

这次的容器都加了 `--init`，sdist 解包到家目录，前一节 `test_command_hook_is_killed_on_timeout` 的失败没有再出现。macOS 沙箱验收第一次拒绝运行，因为虚拟环境在临时目录里，这是脚本的保护；换到家目录下新建的目录后通过。

33 个 live 场景里没有要求模型写沙箱只读位置的任务，这次修复靠的证据不是这一行，真实模型的结果在回归记录里。11-04 第 2 次失败的插话、暂停恢复两条这次也没有复现，原因仍未定位。

PR 上的 CI 在 `890ffea` 全部通过，`src/` 和候选代码相同：Ubuntu 上 Python 3.11～3.13、macOS 上 3.12 的离线套件，两个平台的沙箱验收。

这份记录仍然不代表已经发布：PyPI、TestPyPI 的发布者配置、GitHub environment 和 `v0.2.0` 标签都还没做。
