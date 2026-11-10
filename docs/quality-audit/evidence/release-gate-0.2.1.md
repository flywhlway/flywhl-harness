# 0.2.1 发布门禁记录

日期：2026-11-06。候选代码：`f175563`，内容是 `v0.2.0` 加三个修复和版本号。之后只改了这份记录和 CHANGELOG 的文字。

这是安全补丁，分支 `release/0.2.1` 从 `v0.2.0` 拉出，不含 `main` 上之后的改动。漏洞说明见[安全公告](https://github.com/flywhlway/flywhl-harness/security/advisories/GHSA-7cw9-49p4-27vj)。

和 `v0.2.0` 相比，`src/` 只改了 `config.py`、`prompts.py` 和版本号，新增 `tests/test_project_files.py`。`scripts/`、`pyproject.toml`、`uv.lock`、工作流都没有变。

## 结果

| 检查 | 环境 | 结果 |
|---|---|---|
| 离线套件 | 一次性 Linux 容器，Python 3.12.16，断网 | 878 passed，3 skipped |
| 系统沙箱用例 | 同上 | 21 passed，12 skipped |
| 审计不变量 | 同上 | 12 passed，1 skipped |
| kill -9 恢复示例 | 同上 | 通过 |
| sdist 解包后测试 | 同上 | 810 passed，5 skipped；系统沙箱用例 21 passed，13 skipped |
| wheel 干净安装冒烟 | 一次性 Linux 容器，Python 3.12，按 wheel 声明安装依赖 | 通过，`flywhl-harness 0.2.1` |
| 这次新增的用例 | macOS，Python 3.11.15、3.12.13、3.13.12 | 各 30 passed |
| `twine check --strict` | twine 7.0.0 | wheel、sdist 通过 |
| `pip-audit`（运行依赖，含 anthropic 可选依赖） | 2.10.1 | 没有已知漏洞 |
| 工作流静态检查 | actionlint | 没有告警 |
| `v0.2.0` 之后的提交密钥扫描 | gitleaks 8.30.1 | 0 条命中 |
| live 场景 | 一次性 Linux 容器，真实 bubblewrap，deepseek-flash | 25 passed，8 skipped，291,289 token，0.0450 美元，190 秒 |
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
| `AGENTS.md` 链接指向 `/proc/self/environ` | 发出 | 没发出，有告警 |
| 对照：`SKILL.md` 是目录，`AGENTS.md` 正常 | 会话起不来，退出码 1 | 发出，运行正常 |

用 0.2.0 建的会话跨版本恢复：

| 操作 | 哨兵 |
|---|---|
| 0.2.0 上建会话 | 发出，也写进了 `harness.db` |
| 0.2.1 上恢复这个会话 | **仍然发出** |
| 0.2.1 上新建会话 | 没发出 |

系统提示词在建会话时冻结，升级不会重新过滤。这一点写进了 CHANGELOG 和公告。

## 已知限制

项目边界是最近的 git 根。家目录本身是 git 仓库时，它下面没有自己 `.git` 的工作区，边界是整个家目录：凭证位置仍然被拦，其他文件不拦。`git clone` 下来的仓库有自己的根，不受影响。这一点写进了公告。

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

## 发布结果

标签 `v0.2.1` 指向 `7431be9`，[发布流水线](https://github.com/flywhlway/flywhl-harness/actions/runs/42242245845)第 2 次尝试全部成功。第 1 次有两个 macOS 冒烟作业排了 15 分钟没拿到运行器，被取消，`pypi` 没有执行。第 2 次只重跑了这两个作业和下游，没有改代码。

| 检查 | 环境 | 结果 |
|---|---|---|
| 离线套件 | GitHub 运行器，Linux，Python 3.11、3.12、3.13 | 各 880 passed，1 skipped |
| 系统沙箱用例 | 同上 | 各 21 passed，12 skipped |
| 审计不变量 | 同上 | 各 12 passed，1 skipped |
| 离线套件 | GitHub 运行器，macOS，Python 3.12 | 881 passed |
| 系统沙箱用例 | 同上 | 27 passed，6 skipped |
| 审计不变量 | 同上 | 13 passed |
| 沙箱验收 | GitHub 运行器，macOS / Linux | 23/23 / 23/23 |
| wheel 冒烟 | GitHub 运行器，两个平台，Python 3.11～3.13 | 全部通过 |
| 从 PyPI 干净安装 | macOS 和一次性 Linux 容器，Python 3.11～3.13 | 版本号正确，不带令牌 401，带令牌 200 |
| PyPI 版本的端到端 | 一次性 Linux 容器，Python 3.12，假模型服务 | 13 个场景和上文 0.2.1 一列相同 |

PyPI 文件在 2026-11-06 15:40（UTC）上传，摘要和流水线产物、TestPyPI 都一致：

| 文件 | SHA-256 |
|---|---|
| `flywhl_harness-0.2.1-py3-none-any.whl` | `3e2243193a4be99fe88d50c9b39b109d9d09da7dce327f1839887ef76b698303` |
| `flywhl_harness-0.2.1.tar.gz` | `4bcb924ed41d3900a654ac27fb6acf76d176d0d61e75e680db080477ee6abc81` |

两个文件各有一份来源证明，发布者是本仓库 `release.yml` 的 `pypi` 环境。[GitHub Release](https://github.com/flywhlway/flywhl-harness/releases/tag/v0.2.1) 的附件是这两个文件，说明和 CHANGELOG 对应一节相同。公告在 2026-11-06 15:46（UTC）公开，公开时没有 CVE 编号，也还没有进入 GitHub Advisory Database。

## 没有闭环的事

- **逐行审。** 三个修复提交涉及信任和权限语义。维护者打标签前查看了 diff，没有确认逐行审完。合入 `main` 的 [PR](https://github.com/flywhlway/flywhl-harness/pull/37) 由维护者委托 agent 评审和合并。修复和两次审查是同一个 agent 做的，没有独立审查。
- **已发布版本里的遗留问题。** 0.2.1 上，项目文件是符号链接、目标在进不去的目录里时，会话仍然起不来。这是合入 `main` 时查出的，修复在下一个版本。
- **#12。** 修复合入 `main` 后，开着的 [#12](https://github.com/flywhlway/flywhl-harness/pull/12) 会有冲突，解法已经在本地验证过。
- **live 和基准。** 8 个 live 场景和基准回归，上文列出的缺口在发布后仍然没有证据。
- **模型费用。** 这轮 live 和语言检查约 0.05 美元，加上之前的运行累计约 2.00 美元，到了确认的额度线。再跑花钱的检查要维护者同意。

标签由维护者打，`pypi` 环境由维护者批准。
