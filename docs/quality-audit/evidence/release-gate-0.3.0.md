# 0.3.0 发布门禁记录

门禁日期：2026-11-18，发布日期：2026-11-19。候选代码：`965372b`，内容是 `main`（`b337a50`）加版本号和 `bench/regression.toml` 的编号。之后的提交只改文档和覆盖率基线，`src/`、`scripts/`、`pyproject.toml`、`uv.lock`、工作流都和候选相同。

和 `v0.2.1` 相比，`src/` 包含之后合入 `main` 的全部改动，内容见 [CHANGELOG](../../../CHANGELOG.md) 的 0.3.0 一节。

门禁先在 `c332fd9` 加版本号上跑过一轮，查出两个问题（见下文），修复合入后，全部检查在 `965372b` 上重跑。下表只记重跑的结果。

## 结果

| 检查 | 环境 | 结果 |
|---|---|---|
| 离线套件 | macOS 独立克隆（真实检出，不是 worktree），Python 3.11.15、3.12.13、3.13.12，`FLYWHL_REQUIRE_SANDBOX=1` | 各 2231 passed，1 skipped |
| 系统沙箱用例 | 同上 | 各 37 passed，7 skipped |
| 审计不变量 | 同上 | 13 passed |
| 离线套件 | 一次性 Linux 容器，Python 3.11、3.12.16、3.13，bubblewrap | 各 2228 passed，4 skipped |
| 系统沙箱用例 | 同上 | 各 32 passed，12 skipped |
| 审计不变量 | 同上 | 12 passed，1 skipped |
| kill -9 恢复示例 | 同上 | 通过，外部通知恰好 1 次 |
| Anthropic SDK 离线探针 | 同上，anthropic 2.8.0 | 通过 |
| sdist 解包后测试 | 一次性 Linux 容器 | 2108 passed，14 skipped；系统沙箱用例 32 passed，16 skipped |
| 构建与元数据 | macOS，`uv build`，`twine@7.0.0 check --strict` | wheel 和 sdist 都通过 |
| 干净安装冒烟 | macOS，新建 Python 3.12 venv 只装 wheel | 4 项全部通过，`flywhl --version` 为 0.3.0 |
| 沙箱验收（`--internet`） | macOS 干净 venv；一次性 Linux 容器 | 两边都是 23/23 |
| 覆盖率 | 一次性 Linux 容器 | 整体 89.95%，全部关键模块高于基线，见下文 |
| 运行依赖漏洞 | `pip-audit@2.10.1`，`uv export --frozen --no-dev --extra anthropic` | 没有已知漏洞 |
| 站点依赖漏洞 | `npm audit --prefix docs` | 6 个（2 low、3 moderate、1 high），见“没有闭环的事” |
| 工作流静态检查 | `actionlint` | 通过 |
| live 场景 | 一次性 Linux 容器，真实 bubblewrap，`deepseek:deepseek-flash`，只传 DeepSeek 密钥 | 29 passed，9 skipped，272,451 token，0.0358 美元，146 秒；日志里没有密钥 |
| 回答语言跟随用户 | 一次性 Linux 容器，`deepseek:deepseek-flash` | 英文提问用英文答，中文提问用中文答 |
| 基准彩排 | 一次性 Docker 容器，脚本化模型，提交 `0293407` | 符合预期：60 条试验，普通任务两种配置都过，5 个沙箱敏感任务只有关沙箱才过；回放 60 条分叉 0；脱敏命中 0 |
| 基准回归 `2026-11-r4` | 一次性 Docker 容器，`deepseek:deepseek-flash`，提交 `0293407`（代码和候选相同），30 个任务 × 2 种配置 × 3 次 | 普通任务两种配置都是 75/75；5 个沙箱敏感任务开沙箱 0/3、关沙箱 3/3；15 个失败都是沙箱边界，harness 缺陷 0；回放新轨迹 180 条、已发布轨迹 555 条，分叉都是 0；1.00 美元。见[回归记录](bench-regress-2026-11-18.md) |
| 全历史密钥扫描 | `scripts/secret_scan.sh`，gitleaks v8.30.1，GitHub 上全部引用 | 246 个提交，命中 1 条，是测试假值，见[扫描记录](secret-scan-2026-11-18.md) |
| CI | `tests.yml` | `main` 上 `b337a50` 的运行全部通过（macOS 沙箱验收第一次因 runner 排队被取消，重跑通过）；发版 PR [#54](https://github.com/flywhlway/flywhl-harness/pull/54) 的必需检查在合入前全部通过 |

live 跳过的 9 项都缺密钥：Kimi 2 项、通义千问 2 项、MiniMax 1 项、OpenRouter 2 项、降级链 1 项（降级模型是通义千问）、博查 1 项。

## 覆盖率

在一次性 Linux 容器里算，和 CI 的 `coverage-audit` 作业同一种环境，分支覆盖一起计入。`main` 上 `b337a50` 的 CI 产物整体 89.96%、`tools/shell.py` 98.31%，其余各项和本地相同。

| 模块 | 旧基线 | 候选 |
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

新基线取 CI 产物的数值，整体和 `tools/shell.py` 取本地和 CI 中较低的一个，免得本机 `ci.sh` 因环境差异超出 0.5 个百分点的容差。

## 门禁查出并修掉的问题

| 问题 | 不修的后果 | 处理 |
|---|---|---|
| `tests/test_web_eval.py` 在模块顶层导入 `bench.web_eval`，而 sdist 不带 `bench/` | 下游打包者解包后跑测试，收集阶段就中止，一个用例都跑不了 | [#52](https://github.com/flywhlway/flywhl-harness/pull/52)，和 `tests/test_bench.py` 一样 `importorskip` |
| 锁文件里的 `opentelemetry-api` 1.45.1 有 CVE-2026-126360 | `release.yml` 的 pip-audit 步骤发现已知漏洞即停，打了标签也发不出去 | [#53](https://github.com/flywhlway/flywhl-harness/pull/53)，升到 1.45.2 |

## 发布结果

发版 PR [#54](https://github.com/flywhlway/flywhl-harness/pull/54) 以 squash 合入 `main`，得到 `cc2cc8a`，它的树和跑门禁的 PR 头 `94acc8b` 相同。合入前 PR 上的 macOS 沙箱验收排了 15 分钟没拿到运行器，被取消，只重跑这一个作业后通过。

标签 `v0.3.0` 指向 `cc2cc8a`，[发布流水线](https://github.com/flywhlway/flywhl-harness/actions/runs/44422382283)第 1 次尝试全部成功。

| 检查 | 环境 | 结果 |
|---|---|---|
| 离线套件 | GitHub 运行器，Linux，Python 3.11、3.12、3.13 | 各 2230 passed，2 skipped |
| 系统沙箱用例 | 同上 | 各 32 passed，12 skipped |
| 审计不变量 | 同上 | 各 12 passed，1 skipped |
| 离线套件 | GitHub 运行器，macOS，Python 3.12 | 2231 passed，1 skipped |
| 系统沙箱用例 | 同上 | 37 passed，7 skipped |
| 审计不变量 | 同上 | 13 passed |
| 覆盖率棘轮 | GitHub 运行器，Linux | 通过 |
| 沙箱验收 | GitHub 运行器，macOS / Linux | 23/23 / 23/23 |
| wheel 冒烟 | GitHub 运行器，两个平台，Python 3.11～3.13 | 全部通过 |
| 从 PyPI 干净安装 | macOS（Python 3.11.15、3.12.13、3.13.12）和一次性 Linux 容器（`python:3.x-slim`，3.11.18、3.12.15、3.13.18） | 版本号都是 0.3.0，`flywhl serve` 不带令牌的请求返回 401 |

GitHub 运行器上 Linux 的离线套件比本地一次性容器多过 2 项、少跳过 2 项，是环境差异，两边都没有失败。

PyPI 文件在 2026-11-18 23:43（UTC）上传，`pypi` 环境由维护者批准。摘要和流水线产物、TestPyPI 都一致：

| 文件 | SHA-256 |
|---|---|
| `flywhl_harness-0.3.0-py3-none-any.whl` | `e50381e2cc49531987297ab676527d6cb77d5de219cca2e960078c54e942d64d` |
| `flywhl_harness-0.3.0.tar.gz` | `57c2a95e076a917d76b5642490ff4add3e6581f331ba4180f97f82060e06aee1` |

两个文件各有一份来源证明，发布者是本仓库 `release.yml` 的 `pypi` 环境。PyPI 页面的说明是英文 README，`Documentation` 指向 `/en/`。[GitHub Release](https://github.com/flywhlway/flywhl-harness/releases/tag/v0.3.0) 不是预发布，附件是这两个文件，说明和 CHANGELOG 对应一节相同。站点由 `cc2cc8a` 部署，中文首页写“已发布 0.3.0”，英文首页写“released 0.3.0”。

PR 合入和打标签由 agent 按维护者的授权完成，`pypi` 由维护者在运行页面批准。

## 没有闭环的事

- **放行判定没有改。** [release-readiness.md](../release-readiness.md) 仍是首轮的判定。PR #4 的 `src/` 改动还没有逐行审，Dependabot 安全更新和私密漏洞报告也没开，改判留到这些做完之后。
- **9 个 live 场景没有这个版本的证据。** 只传了 DeepSeek 一家的密钥，上面列出的其他供应商、降级链和博查场景都跳过了。
- **模型结尾没有正文时，用户拿不到说明。** 基准回归里 1 次试验（`env-local-api` 开沙箱 #1）在被沙箱拦下后，最后一个回合只有思考就结束了，Run 记为完成。180 次里只有这 1 次。harness 只在回复因长度上限截断、只剩思考时追问，最多 3 次（`agent/loop.py`），模型正常结束回合、没有正文时不追问。
- **站点依赖的 6 个 npm 漏洞。** 都在文档站的构建工具链里（vite、esbuild、vitepress、vitepress-plugin-mermaid、mermaid、katex），不进 Python 包。vite 和 esbuild 的几条针对开发服务器，发布的站点是构建出的静态页面；katex 那条要先有原型污染才能利用。`npm audit fix --force` 要把 mermaid 升到不兼容的大版本，这次不动。
