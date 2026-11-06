# 0.2.1 发布门禁记录

日期：2026-11-06。候选代码：`8c7d7b3`，内容是 `v0.2.0` 加两个修复和版本号。之后只加了这份记录和 CHANGELOG 里的公告链接。

这是安全补丁，分支 `release/0.2.1` 从 `v0.2.0` 拉出，不含 `main` 上之后的改动。漏洞说明见[安全公告](https://github.com/flywhlway/flywhl-harness/security/advisories/GHSA-7cw9-49p4-27vj)。

和 `v0.2.0` 相比，`src/` 只改了 `config.py`、`prompts.py` 和版本号，新增 `tests/test_project_files.py`。`scripts/`、`pyproject.toml`、`uv.lock`、工作流都没有变。

## 结果

| 检查 | 环境 | 结果 |
|---|---|---|
| 离线套件 | 一次性 Linux 容器，Python 3.12.16，断网 | 868 passed，3 skipped |
| 系统沙箱用例 | 同上 | 21 passed，12 skipped |
| 审计不变量 | 同上 | 12 passed，1 skipped |
| kill -9 恢复示例 | 同上 | 通过 |
| sdist 解包后测试 | 同上 | 800 passed，5 skipped；系统沙箱用例 21 passed，13 skipped |
| wheel 干净安装冒烟 | 一次性 Linux 容器，Python 3.12，按 wheel 声明安装依赖 | 通过，`flywhl-harness 0.2.1` |
| 这次新增的用例 | macOS，Python 3.11.15、3.12.13、3.13.12 | 各 20 passed |
| `twine check --strict` | twine 7.0.0 | wheel、sdist 通过 |
| `pip-audit`（运行依赖，含 anthropic 可选依赖） | 2.10.1 | 没有已知漏洞 |
| 工作流静态检查 | actionlint | 没有告警 |
| `v0.2.0` 之后的提交密钥扫描 | gitleaks 8.30.1 | 0 条命中 |
| live 场景 | 一次性 Linux 容器，真实 bubblewrap，deepseek-flash | 25 passed，8 skipped，239,260 token，0.0306 美元，121 秒 |
| 回答语言跟随用户 | 同上 | 英文提问的回答没有汉字，中文提问回答中文 |

## 端到端核对

在一次性 Linux 容器里真实运行 `flywhl run`，模型地址指向容器内的假服务，记录请求内容，再看每个场景的哨兵字符串有没有发出去。没有信任过工作区，没有真实密钥。

| 场景 | 0.2.0 | 0.2.1 |
|---|---|---|
| 对照：普通项目文件 | 发出 | 发出 |
| 对照：链接指向项目内普通文件 | 发出 | 发出 |
| `AGENTS.md` 链接指向凭证位置 | 发出 | 没发出，有告警 |
| `CLAUDE.md` 链接指向项目外文件 | 发出 | 没发出，有告警 |
| `AGENTS.md` 链接指向项目内 `.env` | 发出 | 没发出，有告警 |
| `SKILL.md` 链接指向项目外文件 | 发出 | 没发出，有告警 |
| 技能目录链接指向项目外 | 发出 | 没发出，有告警 |
| 用户关掉加载，项目配置打开 | 发出 | 没发出 |
| 项目配置写 `append_system_prompt` | 发出 | 没发出，有告警 |
| 用户关掉加载，项目配置仍写提示词 | 发出 | 没发出，有告警 |
| 缓解办法：`-c` 指定关掉加载的配置 | 没发出 | 没发出 |

## 和发布清单不同的地方

| 清单要求 | 这次的做法 | 原因 |
|---|---|---|
| 标签打在 `main` 上 | 打在 `release/0.2.1` 上 | `main` 上有 0.3.0 的改动，还没有放行 |
| macOS 跑完整离线套件和系统沙箱用例 | 本机只跑不执行命令的新用例，其余看发布流水线 `tests` 作业的 macOS 运行 | 这是旧版本的代码，会真实执行命令的用例不在开发机上跑。流水线的运行器是一次性虚拟机 |
| Python 3.11、3.13 的 Linux 矩阵 | 看发布流水线 | 容器镜像只有 Python 3.12 |
| 沙箱验收，两个平台 23/23 | 沿用 [0.2.0 的结果](release-gate-0.2.0.md)，发布流水线会再跑一次 | 沙箱代码没有改 |
| 33 个 live 场景全部通过 | 25 passed，8 skipped | 只传了 DeepSeek 一家的密钥，这 8 条在这个版本上没有证据 |
| 基准回归 | 没有跑 | 基准的代码和任务都没有用到改动的配置项，任务里没有项目文件和符号链接，改动走不到这些路径。回归约 0.93 美元，超出这轮确认的模型额度 |
| 门禁记录走 PR 合入 `main` 后再打标签 | 记录在标签所在分支上 | 合入 `main` 会提前公开修复。发布后再把记录合入 `main` |

## 已评估的例外：站点依赖

`npm audit --prefix docs` 有 6 条：esbuild、katex、mermaid、vite、vitepress、vitepress-plugin-mermaid，low 2、moderate 3、high 1。和 [0.2.0 的例外](release-gate-0.2.0.md#已评估的例外站点依赖)是同一批包，理由不变：这些包只用来构建站点，不进 wheel 和 sdist。

## 没有闭环的事

- **CI。** 这个分支没有跑过 CI，`tests`、`sandbox-acceptance`、`coverage-audit` 和各平台冒烟都要等标签触发的发布流水线。批准 `pypi` 之前要确认它们全部通过。
- **逐行审。** 两个修复提交涉及信任和权限语义，由维护者在打标签前审。
- **修复进 `main`。** 发布后再走 PR，和开着的 #12 有冲突，解法已经在本地验证过。
- **发布结果。** 标签、运行链接、PyPI 摘要和来源证明，发布后补到这份记录里。

这份记录不代表已经发布。标签由维护者打，`pypi` 环境由维护者批准。
