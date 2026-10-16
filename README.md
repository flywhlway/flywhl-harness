# flywhl-harness

[![tests](https://github.com/flywhlway/flywhl-harness/actions/workflows/tests.yml/badge.svg)](https://github.com/flywhlway/flywhl-harness/actions/workflows/tests.yml)

> **Agent = Model + Harness.** 模型负责“想”，harness 负责其余的一切。

一个用 Python 从零手写的最小 Agent Harness：核心不依赖任何 Agent 框架，约 10K 行（其中大量是中文注释），
把 harness 工程里最关键的机制都做成了 **可运行、可测试、可替换** 的代码，并配一个 Agent 控制台（Web UI）与一套系列教程。

```
                 ┌────────────── Harness ──────────────────────────────────────────────┐
  用户 / UI ──►  │ Loop ─ 工具 ─ 上下文 ─ 状态 ─ 生命周期 ─ 策略 ─ 执行环境 ─ 度量  │ ──► 模型
                 └─────────────────────────────────────────────────────────────────────┘
```

## 它实现了什么

| 维度 | 机制 | 代码 |
|---|---|---|
| **Loop** | async generator 主循环；阶段化（整形→模型→落盘→工具→守卫→续跑）；插话 / 排队 | `agent/loop.py` |
| **模型接入** | 统一消息与流事件；`<供应商>:<模型>` 写法与供应商档案（DeepSeek 默认、百炼、Kimi、智谱、MiniMax、OpenRouter、Ollama、Anthropic 官方 SDK）；思考内容按各家规则回传；错误分类（含余额不足、模型不存在）、重试 + 全抖动、熔断、跨供应商降级链；`flywhl doctor` 接入自检 | `llm/` |
| **工具** | 副作用三级（read/write/external）；schema 校验与可操作错误；只读并发、写操作串行、原序回填；大结果外置句柄；MCP 接入 | `tools/`、`agent/executor.py` |
| **上下文** | 水位估算（真实 usage + 增量估算）；旧结果清理（水位线）；摘要压缩（不切开工具配对、增量更新、确定性保留原始任务）；计划复述 | `agent/context.py`、`agent/compaction.py` |
| **状态** | Session / Entry / Run / Step / Event 四层 + SQLite WAL；会话树与分支；只追加 | `runtime/store.py`、`runtime/session.py` |
| **生命周期** | 14 态状态机 + CAS 迁移（接管时钉住观察到的版本）；协作式取消（可中断/不可中断/结果未知）；暂停恢复 + 漂移说明；失败重试；心跳租约 + 周期性崩溃接管（kill -9 后副作用恰好一次）；关闭服务时暂停、重启后继续 | `runtime/state.py`、`cancel.py`、`runtime/manager.py` |
| **策略** | 四种权限模式；deny>ask>allow 规则；命令风险分析（剥包装命令、递归分析 `sh -c`、工作区边界、灾难性命令任何模式都拒绝）；文件工具与命令分析共用一份凭证路径清单；污点感知（跨暂停持久）；Hooks（含与 Claude Code 协议兼容的进程外 hook）；带变更预览的持久化人审 | `policy/` |
| **执行环境** | 路径约束与逃逸检测；影子 git 快照 / 回滚；密钥不进子进程；端云幂等指令下发 | `runtime/workspace.py`、`runtime/dispatch.py` |
| **多 Agent** | 子代理（隔离上下文 + 回传契约 + 违约修正）；DAG 调度（并发配额、失败策略、关键路径） | `agent/subagent.py`、`runtime/scheduler.py` |
| **度量** | 轨迹导出 + OTel GenAI 风格 span；确定性回放 / 反事实分叉；评测（Wilson 区间、McNemar 检验、过早停止率） | `observe/` |

## 三分钟上手

```bash
cd flywhl-harness
uv venv && uv pip install -e ".[dev,anthropic]"     # 或 python -m venv .venv && pip install -e ".[dev,anthropic]"

.venv/bin/flywhl serve -w playground                # 打开 http://127.0.0.1:8765
```

没有任何 API Key 时自动使用内置的 **flywhl-demo** 演示模型——点击开始页的演示场景即可体验审批、取消、压缩、子代理、
循环检测等全部机制。

控制台包含：会话工作区（流式对话、工具调用卡片、带 diff 预览与可编辑参数的审批卡片、暂停 / 停止 / 继续 / 重试、插话与排队）、
检查器（运行概览与状态流转、上下文水位、事件流与断线续传演示、会话分支与快照回滚）、跨会话的运行记录、审批收件箱、工具目录，
以及命令面板（`⌘K` / `Ctrl+K`）。不依赖任何外部字体或 CDN，离线可用；明暗主题、键盘操作与屏幕阅读器均已适配。

接入真实模型（默认 `deepseek:deepseek-flash`）：

```bash
cp .env.example .env                 # 填入密钥；flywhl 启动时从当前目录的 .env 读取密钥与模型选择
.venv/bin/flywhl doctor              # 对每个配置了密钥的供应商跑一次流式文本 + 工具往返
.venv/bin/flywhl serve -w playground # 有 DEEPSEEK_API_KEY 时 auto 模式即用 deepseek:deepseek-flash
.venv/bin/flywhl chat --model kimi:kimi-k2.6                 # 其它写法：dashscope:qwen3.8-flash、minimax:MiniMax-M2.7、
                                                             # anthropic:claude-opus-5-5、ollama:qwen3.5:9b-mlx
```

## 命令行

```bash
flywhl serve                     # Web UI
flywhl chat                      # 终端交互（Ctrl-C 取消当前运行）
flywhl run "列出目录" --json      # 一次性执行，输出 JSONL 事件流
flywhl runs / sessions           # 查看运行与会话
flywhl resume <run_id>           # 恢复暂停/中断的运行
flywhl retry <run_id>            # 重试失败的运行（从历史继续，attempt + 1）
flywhl recover                   # 接管崩溃的运行
flywhl replay <run_id> [--fork-at N]   # 确定性回放 / 反事实分叉
flywhl trace <run_id> -o t.json  # 导出轨迹与 span
flywhl tools                     # 工具与副作用等级
flywhl doctor [供应商:模型 ...]   # 模型接入自检
```

## 作为库使用

```python
import asyncio
from typing import Annotated
from flywhl_harness import Harness, HarnessConfig, HookEvent, HookOutput, Effect, tool

@tool(effect=Effect.READ)
async def weather(city: Annotated[str, "城市名"]) -> str:
    """查询城市天气。"""
    return f"{city}：晴，23℃"

async def main():
    async with Harness(HarnessConfig.load(workspace="playground")) as h:
        h.tools.register(weather)

        @h.hooks.on(HookEvent.PRE_TOOL_USE, matcher="bash")
        async def no_rm(inp):
            if "rm " in inp.tool_input["command"]:
                return HookOutput(decision="deny", reason="禁止删除文件")

        result = await h.run("上海天气怎么样？", on_event=print)
        print(result.status, result.text)

asyncio.run(main())
```

## 示例（全部无需 API Key）

| 示例 | 演示 |
|---|---|
| `examples/01_minimal_loop.py` | 自定义工具、并行只读调用、五种异常都变成模型可读的错误 |
| `examples/02_crash_resume.py` | **真实 kill -9**：工具执行中途杀进程，重启后对账，外部副作用恰好一次 |
| `examples/03_cancel_semantics.py` | 流式中途取消、可中断/不可中断工具的取消差异 |
| `examples/04_context_compaction.py` | 大结果外置 → 旧结果清理 → 摘要压缩，逐轮打印水位 |
| `examples/05_hooks_permissions.py` | 命令风险分析、四种权限模式、hook 改写/拒绝、Stop 验收、进程外 hook、人审改参 |
| `examples/06_subagent.py` | 子代理隔离上下文、回传契约违约修正 |
| `examples/07_swarm_dag.py` | DAG 调度：并发配额、失败只跳过下游、关键路径 |
| `examples/08_replay_eval.py` | 轨迹与 span、确定性回放、定位分叉、带置信区间的评测 |
| `examples/09_edge_cloud.py` | 丢包/重复/乱序网络下的幂等指令下发 |
| `examples/10_long_running.py` | 长时任务：功能清单 + 进度文件 + 每会话一个单元 |
| `examples/11_kill9_cli_recover.py` | **跨进程接管**：`flywhl run` 被 kill -9，另一个 `flywhl recover` 进程接管；打印完整状态迁移轨迹并核对 7 条不变量（CI 每次都跑） |

```bash
.venv/bin/python examples/11_kill9_cli_recover.py
.venv/bin/python -m pytest -q          # 240 个离线测试，包含真实 kill -9 恢复测试与录制的各家真实 SSE 流
```

`examples/11_kill9_cli_recover.py` 的输出节选（不需要 API Key，结果确定）：

```
━━ 崩溃现场
  run.status = running  worker = w38180-37b3e8  心跳停在 0.2s 前  version = 2
  step send_notification  status = started    effect = external 幂等键 = r_…:call_…_0
  外部通知数 = 1
━━ 状态迁移轨迹（每一次迁移都是一条 status 事件）
   1.      created → queued       v1   已创建，等待执行
   2.       queued → running      v2   开始执行
   3.      running → interrupted  v3   Worker w38180-37b3e8 心跳超时
   4.  interrupted → queued       v4   恢复执行
   5.       queued → running      v5   开始执行
   6.      running → completed    v6   自然完成
  step send_notification：reconciled  [对账] 外部系统确认该通知已发送给 ops-team，未重复发送。
```

CI（`.github/workflows/tests.yml`）在 Ubuntu × Python 3.11 / 3.12 / 3.13 与 macOS × 3.12 上跑离线测试和这个演示；
真实模型测试要花钱、要密钥，不进 CI。2026-10-16 本机复现（macOS，Python 3.12.13）：离线 240 项全部通过。

## 真实模型测试

离线测试用脚本化模型验证 harness 的逻辑；协议细节与模型行为只有真实模型能回答。`tests/live/` 覆盖 33 个真实场景：
接入层（思考开关、前缀缓存、截断、错误分类、降级链与项目配置的降级链、多供应商、Anthropic 协议）、Agent 机制（多步工具、并行调用、审批与拒绝理由、
Stop 验收、计划、规划模式、子代理契约、上下文压缩、预算收尾、提示注入、大结果句柄、跨供应商续聊）、运行时（流式中途取消、插话、
暂停恢复、kill -9 接管、真实轨迹回放、Web API 端到端）。默认不运行：

```bash
.venv/bin/python -m pytest -m live tests/live                       # 读取 .env，没有密钥的用例自动跳过
FLYWHL_LIVE_MODEL=kimi:kimi-k2.6 .venv/bin/python -m pytest -m live tests/live   # 换一个被测模型
```

2026-10-16 复现：33 个用例全部通过（约 34 万 token、0.05 美元）。此前本机记录的 30 个失败逐项排查过：
缺密钥 0 个（没有密钥只会跳过，不会失败）；当时的运行环境问题 29 个（同样的密钥重跑即通过）；真实缺陷 1 个。
复跑过程中 plan 模式用例又暴露出第 2 个真实缺陷。两个缺陷都与 plan 模式有关：执行器记录的副作用等级没带工作区；
规则命中后不再受 plan 模式约束，自动审批时能在规划阶段执行写操作。两个都已修复，并补了离线回归测试。
需要哪些密钥、分类明细见 [tests/live/README.md](tests/live/README.md)。

## 教程

`docs/` 是一个可独立部署的 VitePress 站点：从“什么是 harness”到每个机制的原理、设计取舍与源码精读。

```bash
cd docs && npm install && npm run dev    # 本地预览
npm run build                            # 输出到 docs/.vitepress/dist，可部署到任意静态托管
```

## 目录

```
src/flywhl_harness/
  agent/      loop · executor · context · compaction · budget · inbox · subagent
  llm/        base · profiles · models · openai_compat · anthropic · mock · replay · resilient · doctor
  tools/      base · schema · registry · fs · shell · extra · demo · mcp
  policy/     permissions · bash_analyzer · hooks · approvals
  runtime/    manager · store · session · state · workspace · retry · scheduler · dispatch
  observe/    trace · replay · evals
  server/     FastAPI：REST + SSE（Last-Event-ID 续传）
  web/        Agent 控制台（原生 ES 模块，无构建步骤）：app · lib/{api,dom,icons,meta,ui} · views/{session,timeline,inspector,pages}
  cli.py · config.py · prompts.py · types.py · events.py · cancel.py
```

## 设计取舍

- **核心零框架依赖**：只用标准库 + httpx；FastAPI 只用于 Web 层；Anthropic provider 使用官方 SDK。
- **SQLite 而非 PostgreSQL**：单文件、WAL、事务——足以讲清四层模型与 CAS；生产中替换为 PG + 对象存储 + outbox。
- **串行执行 hooks**：便于参数改写的链式传递（Claude Code 选择并行执行）。
- **默认需要审批**：与 pi 的 “YOLO + 容器” 不同，这里把权限与人审作为可学习的机制实现出来；`bypass` 模式只应在隔离环境中使用。
- **Web 服务有访问控制**：Host 白名单（防 DNS 重绑定）+ 跨站写请求检查；`--host 0.0.0.0` 时自动启用访问令牌。
- **Anthropic 模型默认开启 server-side fallback**（`fallbacks: "default"`），被安全策略拒答时服务端改用推荐的备用模型；
  只在官方端点上发送（经 OpenRouter 等网关调用时自动关闭），可通过 `AnthropicProvider(server_fallback=False)` 关闭。
- **启动目录的 `.env` 只接受密钥与模型选择**：工作区默认就是启动目录，可能是不可信的仓库；它的 `.env` 若能设置 `*_BASE_URL`、
  `HTTPS_PROXY` 或权限模式，就能骗走真实密钥或绕过审批。这类变量只从环境变量或 `--env-file` 显式指定的文件读取。
- **启动目录的 `flywhl.toml` 先信任再生效**：其中执行命令（`hooks`、`mcp_servers`、`verify_command`）、改变请求去向（`base_url`、
  `api_key_env`）、放宽审批（`bypass`、allow 规则）的设置，确认后才生效，信任按“路径 + 内容哈希”记在 `~/.flywhl/trusted.json`；
  模型选择、限额、deny 规则这类设置直接生效。代价是在新仓库里第一次运行要多回答一个问题（或加 `--trust`）；
  用户配置 `~/.flywhl/config.toml` 与 `-c` 指定的文件视为可信。

MIT License
