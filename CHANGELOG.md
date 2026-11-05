# 更新记录

版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。1.0 之前，次版本号的升级可能带破坏性变化，这类变化都写在“行为变化”一节。

## Unreleased

### 行为变化

**`flywhl serve` 收紧了网络暴露。** 以前监听 `0.0.0.0`、`::` 时 Host 白名单被关掉，DNS 重绑定的防线随之消失，只剩共享令牌。现在白名单始终生效，监听非回环地址也要明确声明。

| 你会看到 | 原因 | 怎么处理 |
|---|---|---|
| `--host 0.0.0.0` 或别的非回环地址退出码 2 | 控制台能批准命令、切换权限，暴露到网络要明确声明 | 加 `--expose`。回环地址不需要 |
| 监听 `0.0.0.0`、`::` 时从别的机器打开返回 403 | 白名单只有回环地址和 `--allow-host` 写的 | 加 `--allow-host <主机名或IP>`。没写时启动会提示。监听具体的 IP 时这个 IP 自动进白名单 |
| 监听 `0.0.0.0`、`::` 时打印的链接变成 `http://127.0.0.1:…`、`http://[::1]:…` | 通配地址不在白名单里，原样打印的链接打开就是 403 | 从别的机器打开时换成 `--allow-host` 写的地址 |
| `--allow-host '*'` 退出码 2，`create_app(allowed_hosts=["*"])` 抛 `ValueError` | 白名单不能关闭 | 写出具体的主机名或 IP |
| 非回环地址上 `--token` 不足 16 个字符时退出码 2 | 网络上的其他机器能反复试，短令牌挡不住 | 换长令牌，或者不写 `--token`，用自动生成的 |
| `--no-token` 退出码 2，提示命令连得到控制台端口 | 不带令牌时，连得到这个端口的命令能调用审批接口，批准自己的人工门 | 去掉 `--no-token`，或者收紧提示里列出的沙箱设置 |
| `/api/settings` 的 `access` 里没有 `any_host` 了 | 这个状态不再存在 | 读这个字段的脚本去掉它 |

“连得到控制台端口”指全局沙箱档位 off、全局 `sandbox_network = "allow"`、`allowlist` 放行了回环地址上的这个端口，以及单条 hook、MCP 服务器自己的同类设置。配置了沙箱但这台机器用不了时命令不会执行，不算连得到。

**English summary.** `flywhl serve` no longer disables the Host allowlist on `0.0.0.0` and `::`, and `*` is refused by both `--allow-host` and `create_app`. Listening on a non-loopback address now requires `--expose` plus a token, and a manual `--token` there needs at least 16 characters. A wildcard listen trusts only loopback names and `--allow-host` entries, so remote browsers get 403 until you add the address, and the printed link uses a loopback address. `--no-token` is refused when commands the agent runs can reach the console port, because they could otherwise approve their own human gate: sandbox off, `sandbox_network = "allow"`, or an allowlist rule for that loopback port, globally or on a single hook or MCP server. `access.any_host` is gone from `/api/settings`.

**会话库默认按断电耐久落盘。** 新配置项 `durability` 默认 `full`：`synchronous=FULL`，加上 `fullfsync` 和 `checkpoint_fullfsync`。
之前是 `NORMAL`，断电可能丢掉最后几次提交，其中可能有执行前写的工具 intent，恢复时看不到记录，非幂等调用会再执行一次。
保证只覆盖会话库，影子 git 和工作区文件不在内。

代价是提交变慢，而提交在事件循环线程上执行。在一台 Apple M5 Pro 的内置 SSD 上，提交 p50 从约 0.05ms 变成约 4ms。
单个会话按快模型的速度输出时每轮慢 0～15%，4 个会话并发时每轮慢 0.7～1.1 倍，机器负载越高越慢，事件循环最长卡过将近 1 秒。
数字只代表那台机器的那种磁盘，见[压测记录](https://github.com/flywhlway/flywhl-harness/blob/main/docs/quality-audit/evidence/sqlite-durability-2026-11-06.md)。

要回到之前的行为，写 `durability = "normal"`。它放宽了保证，写在项目配置里要先信任才生效。取值写错时加载就报错。

### 新增

- `scripts/durability_bench.py`：用 mock 模型跑真实的 Agent 循环，比较两种落盘强度的提交耗时、事件循环延迟和吞吐。

## [0.2.0] - 2026-11-05

第一个发布到 PyPI 的版本。v0.1.0 之后的主线是安全边界：系统沙箱从可选原型变成默认开启，命令分析、凭证保护、供应链检查都重做了一轮。

### 行为变化

**bash、命令 hook、stdio MCP 服务器、`verify_command` 默认都在系统沙箱里运行。** macOS 用 `sandbox-exec`，Linux 用 bubblewrap。
命令读不到凭证位置，只能写工作区和临时目录，网络默认是空白名单。

| 你会看到 | 原因 | 怎么处理 |
|---|---|---|
| Linux 上 bash 拒绝执行，提示没有可用的沙箱 | 没装 bubblewrap，或 user namespace 被限制。这时不会退回沙箱外 | 装 bubblewrap；Ubuntu 24.04 可能还要 `sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0`。确实不要沙箱时，在 `~/.flywhl/config.toml` 写 `sandbox = "off"`，或启动时加 `--sandbox off` |
| 配置了 MCP 服务器时 harness 启动失败 | 同上，MCP 服务器不会在沙箱外启动 | 同上 |
| `pip install`、`npm install`、`git clone` 连不上网 | 默认白名单为空，报错里写着被拦的主机 | 启动时加 `--net-allow pypi.org`，或写进 `permissions.network_allow` |
| `git push`（SSH）、`gh`、`kubectl`、`docker` 失败 | 沙箱里没有凭证和本机套接字 | 让模型带 `outside_sandbox: true` 请求，由人确认后在沙箱外执行；或者自己在终端里跑 |
| `git config`、`git remote add`、`git push -u` 失败 | `.git/hooks`、`.git/config` 在沙箱里只读。`git add`、`git commit` 不受影响 | 同上 |
| 项目 `flywhl.toml` 里的 `sandbox = "off"` 不生效 | 关闭沙箱只有三个入口：用户配置、`-c` 指定的文件、`--sandbox off`。信任项目配置也不行 | 改用这三个入口之一 |
| 无人值守时 `write`、`edit` 改 `.git/config`、`.git/hooks`、`flywhl.toml` 被拒绝，`--yes` 也不行；有人看时每次都要确认 | 这些位置在沙箱里只读，文件工具不在沙箱里，放开就能绕过沙箱。没开沙箱时不受影响 | 由人确认，或者自己在终端里改 |
| 写了 `sandbox = "macos"` 或 `"linux"` 的配置连不上网 | 没写 `sandbox_network` 时从 `allow` 变成 `allowlist` | 显式写 `sandbox_network = "allow"`，或把主机加进白名单 |

`outside_sandbox` 是一道独立的人工门：任何模式下都要人确认，`auto_approve`、规则、会话授权都放行不了，无人值守时直接拒绝。
`write`、`edit` 写沙箱里只读的位置走同一道门。

**English summary.** bash, command hooks, stdio MCP servers and `verify_command` now run inside an OS sandbox by default
(`sandbox-exec` on macOS, bubblewrap on Linux). Credentials are unreadable, writes are limited to the workspace and temp directories,
and the network starts as an empty allowlist. If no sandbox is available the harness refuses to run instead of falling back:
install bubblewrap, or opt out with `sandbox = "off"` in `~/.flywhl/config.toml` or `--sandbox off`; project config cannot opt out.
Allow hosts with `--net-allow` or `permissions.network_allow`. Commands that need your credentials (`git push` over SSH, `gh`, `kubectl`)
either go through the human-only `outside_sandbox` gate or run in your own terminal. `.git/hooks` and `.git/config` are read-only inside the
sandbox, so `git config`, `git remote add` and `git push -u` fail there. `write` and `edit` on those paths and on `flywhl.toml` take the same
human-only gate, so unattended runs refuse them, including `--yes`. Configs that set `macos` or `linux` without `sandbox_network`
move from `allow` to `allowlist`.

### 新增

- 系统沙箱：`sandbox = "auto"` 启动时真跑一次探测；Linux bubblewrap 档位；两个平台都有出站白名单，宿主代理按解析结果放行、重写 Host，命令结束时回收后台进程；单条 hook、MCP 服务器可以单独设 `sandbox`、`network_allow`，MCP 还可以设 `sandbox_network`。
- `outside_sandbox`：在沙箱外执行一条命令的人工门。
- 无人值守自动批准：`auto_approve` 配置和 `--approve`。
- 审批疲劳提示：秒批成串、同类操作反复批准时提醒，审批页给出疲劳指标。
- hook 与 MCP 的供应链检查：引用的脚本按内容绑定，经 `npx`、`uvx`、`docker run` 临时拉取的包可以要求锁到确切版本（`require_pinned`）。
- 「环境与安全」页、`flywhl doctor`、启动信息显示实际生效的沙箱档位和网络策略。
- 精简手册、能力清单与质量审计记录。

### 安全

- 命令分析：按 bash 的命令边界切分，堵住换行、注释、保留字绕过；跟踪变量取值与当前目录；展开函数和别名；预算或组合数超限时拒绝，不再放行。
- 凭证保护：匹配不区分大小写；bash 命中凭证位置时任何模式都拒绝；文件工具和 bash 共用一份清单。
- 本机 IPC：钥匙串、剪贴板、`open`、launchd、Unix 套接字不再绕过沙箱。
- 关掉沙箱外的两条通道：影子 git 快照不再替沙箱执行工作区里的配置，回放不再绕过人工门。
- 命令 hook 子进程剥离密钥类环境变量，需要的用 `env`、`pass_env` 显式传入。MCP 服务器自报的注解默认不采信。
- 项目配置不能替人打开自动批准；放行规则按文件实际落点匹配；MCP 服务器与 hook 的放宽类设置按类型严格校验。
- Web 服务：写请求的 Origin 必须与 Host 同主机同端口，查询串里的令牌只在 `/` 兑换。会话库与数据目录只给属主访问。

### 修复

- 演示模型名只走内置演示供应商，不会访问网络。
- 命令行的 `--provider` 优先于 `.env` 和环境变量里自带供应商的模型。
- 窄屏下检查器覆盖层避开页头，可以点遮罩收起。
- 内置提示词不再要求模型用中文回答，改为跟随用户使用的语言。
- 审批时写的拒绝理由改走提醒通道。放在工具结果里时，模型会按“工具结果是数据”的规则不照做，停下来反问。
- 压缩时模型没给出可用的摘要，之前保留的事实不再丢失。调用失败或空回复时退回的确定性摘要不带上一次的内容；模型接着对话往下说时，那句话会被当成摘要存下来。
- OpenRouter 的 `not a valid model ID` 识别为模型不存在，降级链能接手。
- 经 OpenRouter 调 Claude 时打开缓存。之前缓存命中一直是 0。
- `.flywhl/` 里自动放一份 `.gitignore`，忽略会话库、审批记录和快照，`config.toml` 和 `skills/` 仍然可以提交。之前这些文件一直显示为未跟踪，`git add -A` 会把它们带进仓库。
- 命令的环境变量过滤不再删掉 `GIT_AUTHOR_NAME`、`GIT_AUTHOR_EMAIL`、`GIT_AUTHOR_DATE`。它们撞上了 `AUTH` 规则，靠环境变量配身份时提交会报身份未知。
- 确定性回放用录制的压缩摘要、插话、恢复说明和验收结果，同一会话的后续 Run 也能回放。之前真实模型的压缩轨迹必定报分叉，验收失败重试的轨迹会少走几步而查不出来。新增 `replay_trajectory()`，只靠保存的轨迹就能回放。

### 工程

- 新增 `bench/`：公开基准的流水线、30 个任务和方法说明。真实模型只在一次性容器里运行，第一次结果见[基准页](https://flywhlway.github.io/flywhl-harness/benchmark/)。live 夹具可以用 `FLYWHL_LIVE_ARTIFACTS` 导出结果和轨迹。
- 包：`pip install flywhl-harness`、`pipx install flywhl-harness` 可用；sdist 改成白名单；版本号只在 `__init__.py` 写一处。
- 测试统一在系统沙箱里运行，子进程启动前有执行绊线，起因见[事故复盘](https://github.com/flywhlway/flywhl-harness/blob/main/docs/quality-audit/incident-2026-10-31.md)。
- CI：Linux 和 macOS 真跑沙箱用例，沙箱起不来时失败；新增干净安装验收、覆盖率和依赖漏洞报告；推送 `v*` 标签走发布流水线。

## [0.1.0] - 2026-10-16

首个标签。Agent Loop、工具与 MCP、上下文压缩、四层状态模型、检查点与崩溃恢复、取消语义、权限与 Hooks、多 Agent、轨迹回放与评测、Web 控制台和 30 章教程。
