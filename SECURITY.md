# 安全策略

## 报告漏洞

请私下报告，不要在公开 Issue、PR 或讨论里写细节。

入口是 [GitHub Security Advisories](https://github.com/flywhlway/flywhl-harness/security/advisories)：

1. 页面上有 **Report a vulnerability** 按钮时，用它提交，内容只有你和维护者能看到。
2. 没有这个按钮时，开一个 Issue，只写“有安全问题想私下沟通”，不写任何细节。维护者会建一份私密的安全公告草稿，把你加进去。

报告里请写清楚：

- 版本（`flywhl --version`）、操作系统、沙箱和权限模式的配置；
- 越过了下面哪一道边界，实际造成了什么；
- 最小复现步骤。载荷用无害的哨兵，比如在临时目录里创建一个标记文件，不要附真实密钥。

## 支持的版本

只支持最新的 0.x 次版本，修复发在它的新版本里，不回移到旧的次版本。

| 版本 | 状态 |
|---|---|
| 0.2.x | 支持 |
| 0.1.x | 不再支持 |

## 响应预期

项目由个人维护，下面是目标，不是保证：

- 7 天内确认收到；
- 30 天内给出判定：是否算漏洞、打算怎么修。

确认是漏洞时，修复随新版本发布，之后再公开安全公告，行为变化写进 [CHANGELOG](CHANGELOG.md)。不算漏洞时会说明理由。修复发布之前，请不要公开细节。

## 适用范围

支持的用法是本机、单用户的开发工具。不支持多用户共享和多租户服务，控制台也不应该暴露到回环地址之外，这些场景下的问题不算漏洞。

提示注入本身不算漏洞。模型无法在语法层面区分指令和数据，被文件、网页、MCP 结果里的文字说服，这件事降不到零。项目的防线是限制注入成功之后的损失，越过这些防线才算漏洞。

### 算漏洞

| 边界 | 现有行为 | 算漏洞的情况 |
|---|---|---|
| 系统沙箱 | 默认开启，macOS 用 sandbox-exec，Linux 用 bubblewrap。bash、命令 hook、stdio MCP 服务器和 `verify_command` 都在里面 | 里面的进程读到凭证位置；写到工作区和临时目录之外，或写了 `.flywhl/`、`flywhl.toml`、`.git/hooks`、`.git/config`；连到白名单之外的主机；经 docker.sock、ssh-agent、钥匙串、`open` 这类通道让沙箱外的进程代劳；沙箱用不了时退回沙箱外执行 |
| 人工门 | 沙箱外执行（`outside_sandbox`），以及开着沙箱时 `write`、`edit` 写沙箱里只读的位置，都要人当场确认。规则、会话授权、`auto_approve` 和 `--yes` 都放行不了，无人值守时拒绝 | 没经过人确认就执行了；项目配置里的高危设置没经过信任确认就生效；恶意网页绕过控制台的令牌、Host 白名单和 Origin 校验，替人批准操作或切换权限 |
| 凭证拒绝 | [凭证位置](src/flywhl_harness/policy/sensitive.py)在 `read`、`grep`、`write`、`edit` 和 bash 上任何模式都拒绝，包括 `bypass` | 文件工具读写到凭证位置；默认沙箱下 bash 读到凭证内容 |

### 不算漏洞

- 模型被注入后提出危险操作，但被上面的防线拦住，或者是人批准的。
- `bypass`、`--yes` 放行了本来要确认的操作，这是它们的作用；硬拒绝和人工门不受影响。
- `sandbox = "off"` 或 `--sandbox off` 之后，bash 里运行时才拼出来的路径认不出。这是静态分析的上限，要靠沙箱兜底。
- 已公开的缺口：工作区文件不是污点来源；项目指令文件进入系统提示词；密钥环境变量只按变量名剥离；写进工作区的内容之后在沙箱外执行，比如 `Makefile`、`.envrc`、子模块配置。
- 内核漏洞。系统沙箱和宿主共用内核，跑不可信代码时请用容器或 microVM。
- `docs/` 站点依赖的问题。这些包只用来构建站点，不进 wheel 和 sdist。

防线和缺口的完整对照见[安全章节](https://flywhlway.github.io/flywhl-harness/governance/security)。

## English

flywhl-harness is a local, single-user developer tool. Multi-user and multi-tenant use are not supported.

Report vulnerabilities privately through [GitHub Security Advisories](https://github.com/flywhlway/flywhl-harness/security/advisories). If the **Report a vulnerability** button is not shown, open an Issue that only says you have a security problem to discuss, with no details, and a maintainer will open a private draft advisory with you.

Only the latest 0.x minor version is supported; 0.1.x is not. The project has one maintainer, so these are targets rather than guarantees: an acknowledgement within 7 days and an assessment within 30 days.

Prompt injection alone is not a vulnerability. Bypassing the OS sandbox, the human approval gate or the credential denial is.
