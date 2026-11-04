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
