# 更新记录

版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。1.0 之前，次版本号的升级可能带破坏性变化，这类变化都写在“行为变化”一节。

## [0.2.0] - 未发布

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
| 写了 `sandbox = "macos"` 或 `"linux"` 的配置连不上网 | 没写 `sandbox_network` 时从 `allow` 变成 `allowlist` | 显式写 `sandbox_network = "allow"`，或把主机加进白名单 |

`outside_sandbox` 是一道独立的人工门：任何模式下都要人确认，`auto_approve`、规则、会话授权都放行不了，无人值守时直接拒绝。

**English summary.** bash, command hooks, stdio MCP servers and `verify_command` now run inside an OS sandbox by default
(`sandbox-exec` on macOS, bubblewrap on Linux). Credentials are unreadable, writes are limited to the workspace and temp directories,
and the network starts as an empty allowlist. If no sandbox is available the harness refuses to run instead of falling back:
install bubblewrap, or opt out with `sandbox = "off"` in `~/.flywhl/config.toml` or `--sandbox off`; project config cannot opt out.
Allow hosts with `--net-allow` or `permissions.network_allow`. Commands that need your credentials (`git push` over SSH, `gh`, `kubectl`)
either go through the human-only `outside_sandbox` gate or run in your own terminal. `.git/hooks` and `.git/config` are read-only inside the
sandbox, so `git config`, `git remote add` and `git push -u` fail there. Configs that set `macos` or `linux` without `sandbox_network`
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
- 确定性回放用录制的压缩摘要、插话、恢复说明和验收结果，同一会话的后续 Run 也能回放。之前真实模型的压缩轨迹必定报分叉，验收失败重试的轨迹会少走几步而查不出来。新增 `replay_trajectory()`，只靠保存的轨迹就能回放。

### 工程

- 包：`pip install flywhl-harness`、`pipx install flywhl-harness` 可用；sdist 改成白名单；版本号只在 `__init__.py` 写一处。
- 测试统一在系统沙箱里运行，子进程启动前有执行绊线，起因见[事故复盘](docs/quality-audit/incident-2026-10-31.md)。
- CI：Linux 和 macOS 真跑沙箱用例，沙箱起不来时失败；新增干净安装验收、覆盖率和依赖漏洞报告；推送 `v*` 标签走发布流水线。

## [0.1.0] - 2026-10-16

首个标签。Agent Loop、工具与 MCP、上下文压缩、四层状态模型、检查点与崩溃恢复、取消语义、权限与 Hooks、多 Agent、轨迹回放与评测、Web 控制台和 30 章教程。
