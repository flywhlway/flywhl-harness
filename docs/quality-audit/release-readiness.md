# 生产发布就绪度

**判定：未就绪。** 不能作为可被其他本地程序或不可信模型驱动的代理运行时发布。作为“只看演示、工作区里没有凭证、人不批准写操作”的本地玩具，只读路径是通的，但默认安全边界已经有已复现的绕过，所以也不建议把不可信仓库交给当前版本。

## 判定依据

已经成立的部分：

- 离线测试 268 通过，33 个 live 用例按设计排除。
- 干净环境里，kill -9 之后另一个进程接管，外部通知恰好一次，状态迁移符合白名单。
- 隔离控制台上，只读任务能完成；写文件会停在带 diff 的审批卡上；拒绝后工作区没有新文件。
- 跨站 Origin、`Origin: null`、错误 Host 返回 403。小写 `.ssh` 路径仍被拒绝。名字里带 `KEY` 的变量不会出现在子进程的 `echo` 里。

不足以发布的部分：

- QA-001、QA-002、QA-003 在 **default 模式、无人批准** 时成立。这是文档里的默认姿态，不是 bypass 的边角。
- QA-004 让同一台机器上其他端口的网页创建会话。CLI 对回环监听默认不配令牌，令牌也挡不住这个 Origin。
- QA-005、QA-006 破坏“灾难性命令任何模式拒绝”和“mock 不访问网络”这两条对外说明。

## 发布范围

| 用法 | 现在 |
|---|---|
| 本机演示模型，空工作区，只看 UI | 可以演示。不要把含 `.ssh` 或 `.env` 的目录当工作区 |
| 开发者日常使用，default 模式，工作区是自己的仓库 | 不建议，直到 QA-001 和 QA-002 修复。仓库里的凭证文件能被大小写读出 |
| `--yes` 或 bypass 跑不可信任务 | 不行。QA-005 之外，bypass 本来就会放行高危命令 |
| 监听非回环地址给别人用 | 不行。没有用户隔离，Host 白名单在 `0.0.0.0` 时还会关掉 |
| 多租户 SaaS | 不在这个代码的结构里。缺少认证主体、租户边界和独立数据存储 |

## 残余风险（修完 Phase 0 仍然在）

- 命令分析不是安全边界。作者在 `bash_analyzer.py` 里写了同一句话。字符串分类修完之后，仍可能有变量拼接、编码和 `python -c`。那些在 default 模式多数是 ask，在 bypass 是执行。真正的限制要靠默认开启的沙箱，而沙箱今天是关的，且不封网络（QA-008）。
- live 模型行为本次没有证据。
- 覆盖率和依赖漏洞没有扫描结果，不能写成“无已知 CVE”。
- SQLite `synchronous=NORMAL` 不承诺断电不丢最后一次提交。

## 放行条件

同时满足才把就绪度改成“有条件就绪”：

1. `docs/quality-audit/tests` 全部通过，且 `pytest -q` 仍然 268 项量级全部通过（数量可以因新增用例上升，不能靠删断言下降）。
2. QA-004 在真实监听的服务上，用浏览器从另一个回环端口发起 `POST /api/sessions` 得到 403，从控制台自己的页面发起仍得到 200。
3. 示例 11 在环境里留有供应商密钥时通过，并且没有向该供应商发请求。
4. 上述结果写回本目录的 evidence，而不是只改结论段落。

在那之前，整改停留在 [remediation-roadmap.md](remediation-roadmap.md)。

## 整改后复核（2026-10-18）

Top10 修复后按上面四个条件逐条复核，记录见 [fix-report.md](fix-report.md)：

| 条件 | 结果 | 证据 |
|---|---|---|
| 1. 审计套件全部通过，`pytest -q` 不减少 | 13 passed；322 passed（3.11 / 3.12 / 3.13） | [evidence/fix-pytest.txt](evidence/fix-pytest.txt) |
| 2. 真实服务上其他回环端口 403、本页 200 | `127.0.0.1:9999`、`localhost:8768` 为 403；本服务 origin 为 200；从 8768 页面发起的跨端口写请求没有建出会话 | [evidence/fix-uat.json](evidence/fix-uat.json) |
| 3. 示例 11 在留有密钥的环境里通过且不访问供应商 | 假密钥 + 代理指向死端口，七条不变量通过 | 同上 |
| 4. 结果写回 evidence | 已写 | `evidence/fix-*` |

四条都满足，按本文的规则就绪度可改为 **有条件就绪**：限于本机单用户、default 模式。条件是上文“残余风险”仍然成立——沙箱默认关闭且不封网络（QA-008），命令 hook 超时放行（QA-007），bypass / `--yes` 仍不适合跑不可信任务。

## 第二轮加固后复核（2026-10-31）

第二轮加固与能力清单完成后，逐条复核上文的残余风险，记录见 [security-round2.md](security-round2.md)、[capability-backlog.md](capability-backlog.md)：

| 残余风险 | 2026-10-18 | 2026-10-31 |
|---|---|---|
| 命令分析漏判 | 变量拼接、编码、`python -c` 都看不到 | 赋过值的变量按取值展开，`cd` 之后按可能的当前目录拼路径，值要到运行时才知道的按越界处理。编码过的命令和 `python -c` 里的代码仍看不到：default 下要确认，bypass 下照样执行 |
| 沙箱（QA-008） | 默认关闭，不封网络 | 有 macOS、Linux 两档，可断网，macOS 档可只放行白名单主机；钥匙串、docker.sock 这类本机通道被拦住。仍默认关闭，命令 hook 与 MCP 子进程不在沙箱里 |
| 命令 hook 超时（QA-007） | 失败即放行 | `pre_tool_use` 默认失败即拒绝 |
| live 模型行为 | 没有证据 | 仍没有：本轮没跑 live 用例（要花钱，留给人决定） |
| 覆盖率、依赖漏洞 | 没有扫描结果 | 仍没有 |
| SQLite `synchronous=NORMAL` | 断电可能丢最后一次提交 | 未变 |

测试（提交 `81e6e3e`）：
- macOS，Python 3.12：离线 670 passed，33 个 live 用例默认不选；系统沙箱用例 11 passed，6 个 Linux 专用的跳过；
- 一次性 Linux 容器（Python 3.12，bubblewrap 0.12）：667 passed、3 skipped；系统沙箱用例 6 passed，11 个 macOS 专用的跳过；
- 审计不变量套件 13 passed。

判定维持 **有条件就绪**：限于本机单用户、default 模式。跑不可信仓库时，现在有两种比 bypass / `--yes` 稳妥的做法：
- 打开沙箱并断网：`sandbox = "macos"` 或 `"linux"`，`sandbox_network = "deny"`；
- 无人值守时用 `on_ask = "deny"` 加 `auto_approve` 规则，只放行写明的操作。

bypass / `--yes` 仍不适合跑不可信任务。

## 系统沙箱默认开启后复核（2026-11-04）

| 残余风险 | 2026-10-31 | 2026-11-04 |
|---|---|---|
| 沙箱（QA-008） | 有两档，仍默认关闭；Linux 档没有出站白名单；命令 hook、MCP 服务器在沙箱外 | 已关闭。默认开启，用不了时拒绝执行；网络默认是空白名单，两档都有出站白名单；bash、命令 hook、MCP 服务器、`verify_command` 共用一层 |
| 沙箱外的通道 | 没有专门查过 | 独立评审查出影子 git、回放两条，修复前在一次性容器里实证，已修复，见 [issues.md](issues.md) 文末 |
| live 模型行为 | 没跑 | 33 个场景在真实 bubblewrap 里跑过。三次完整运行：两次各 2 条失败，一次全过；其中一次的两条没有定位到原因，见 [tests/live/README.md](../../tests/live/README.md) |

测试（提交 `dd05852`）：
- macOS，Python 3.12：离线 724 passed；系统沙箱用例 26 passed，7 skipped；
- 一次性 Linux 容器（Python 3.12，bubblewrap）：离线 720 passed、4 skipped；系统沙箱用例 21 passed，12 个 macOS 专用的跳过；
- 审计不变量套件：macOS 13 passed，Linux 12 passed、1 skipped；
- 干净安装验收：装 wheel、不写配置，两个平台都是 23/23，含真实公网主机的放行与拦截；
- 开销：每条命令多十几到二十几毫秒，表在 [18 章](../governance/workspace-sandbox.md#开销)。

证据：
- [验收 macOS](evidence/sandbox-acceptance-2026-11-04-macos.json)、[验收 Linux](evidence/sandbox-acceptance-2026-11-04-linux.json)；
- [开销 macOS](evidence/sandbox-overhead-2026-11-04-macos.json)、[开销 Linux](evidence/sandbox-overhead-2026-11-04-linux.json)；
- [测试结果](evidence/sandbox-default-pytest-2026-11-04.txt)、[修复前的失败输出](evidence/sandbox-default-before-fix-2026-11-04.txt)、[live 结果](evidence/live-2026-11-04.txt)。

判定维持 **有条件就绪**：限于本机单用户。这一轮解决的是“默认姿态不安全”，生产放行还要看其余台账。
系统沙箱和宿主共用内核，写进工作区、之后在沙箱外执行的东西也管不到，边界见 [18 章“这一层不管的事”](../governance/workspace-sandbox.md#这一层不管的事)。

## 0.2.0 发布门禁复核（2026-11-05）

结果和例外见[门禁记录](evidence/release-gate-0.2.0.md)：两个平台的离线、系统沙箱、审计不变量、干净安装和沙箱验收全部通过，运行依赖没有已知漏洞，33 个 live 场景通过。

这一轮修了一个 harness 缺陷：审批时人写的拒绝理由被模型当成不可信数据，见 [tests/live/README.md](../../tests/live/README.md)。发布流水线本身有三处问题，也在门禁里查出并修掉了。

判定不变，仍是 **有条件就绪**：限于本机单用户。可以发布 0.2.0 这个范围，不代表生产放行。下面两项这一轮没有处理：

- 工作区文件不算污点来源；
- 项目指令文件进系统提示词。

## 公开基准就绪情况（2026-11-05）

流水线、30 个任务和方法说明草案已经写好。[离线彩排](evidence/bench-rehearsal-2026-11-05.md)通过，[真实模型试跑](evidence/bench-pilot-2026-11-05.md)查出的 harness 缺陷已修复，五家模型的 24 个场景重跑后全部通过或按设计跳过。试跑的数据不计入结果。

2026-11-05 在提交 `333bbb3` 上完成正式运行，方案 `2026-11`，花费 1.90 美元。结果见[基准概览](../benchmark/index.md)和[结果页](../benchmark/results.md)，经过见[正式运行记录](evidence/bench-official-2026-11-05.md)。

结论：**上面“有条件就绪”的判定不变。** Q1 和 Q3 没有查出 harness 缺陷。Q2 的 20 个失败归类后没有 harness 缺陷，但运行中观察到无人值守时文件工具能绕开 `.git/config` 的只读保护。这一项已修复，受影响的两个任务在 `c86ae89` 上重跑过，已发布的 555 条轨迹在同一提交上回放分叉 0，见[回归记录](evidence/bench-regress-2026-11-05.md)。

这次修复改了权限语义，维护者审过后合入 `main`（`dd4331a`）。合入后的候选代码重跑了全部发布门禁，结果见[门禁记录](evidence/release-gate-0.2.0.md)。这一轮基准到此收尾，标签 `milestone-public-benchmark`。

0.2.0 已于 2026-11-05 发布到 PyPI，结果见[门禁记录](evidence/release-gate-0.2.0.md)。这个版本的 `src/` 和 `c86ae89` 相同：回归记录适用于这份代码，正式结果绑定 `333bbb3`，两者的差别只有这次修复。

还没有做的：

- 方法说明是按开跑指令定稿的，全文和 30 个任务的代表性没有单独审阅；
- 概览页的措辞没有单独审阅；

## 会话库断电耐久复核（2026-11-06）

上文“`synchronous=NORMAL` 不承诺断电不丢最后一次提交”这项残余风险，代码上已经处理：`durability` 默认 `full`，`normal` 是退路。保证只覆盖会话库，影子 git 和工作区文件不在内。

默认保留 `full`，这超过了事先定的判定线：macOS 首轮 4 种情况有 3 种超标；重测 `paced` 时单会话三项都过，4 个会话吞吐仍下降约 40%；Linux 容器 4 种全部超标。保留的理由是单会话接近真实节奏时代价可接受，变慢能恢复，断电后重复执行非幂等工具不能恢复。要吞吐的人有 `durability = "normal"`。数字和没有采用的方向见[压测记录](evidence/sqlite-durability-2026-11-06.md)。

没有做真实的断电实验，慢磁盘也没有测。
