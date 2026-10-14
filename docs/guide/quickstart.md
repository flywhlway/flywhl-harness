# 02 快速上手

这一章把 flywhl-harness 跑起来，并带你认识 Web UI 上每一块面板在展示什么——后面每一章讲到的机制，
几乎都能在这些面板上“看见”。

## 安装

需要 Python 3.11+。推荐用 [uv](https://docs.astral.sh/uv/)：

```bash
cd flywhl-harness
uv venv
uv pip install -e ".[dev,anthropic]"
```

不用 uv 也可以：

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev,anthropic]"
```

`anthropic` extra 只在使用 Anthropic provider 时需要；核心包只依赖 `httpx`，Web 层依赖 `fastapi` 与 `uvicorn`。

## 启动 Web UI

```bash
.venv/bin/flywhl serve -w playground
```

打开 `http://127.0.0.1:8765`。`-w` 指定 **工作区**：Agent 的所有文件操作都被约束在这个目录里，
数据（会话库、快照、演示用的“外部系统”）存放在 `playground/.flywhl/`。

没有设置任何 API Key 时，harness 会自动使用内置的规则演示模型 **flywhl-demo**。它不“智能”，但会按固定剧本调用工具，
足以把各种机制完整地演示出来。开始页的“演示场景”卡片（以及输入框左下角的“模板”菜单）对应这些剧本：

| 说法 | 你会看到的机制 | 相关章节 |
|---|---|---|
| `列出目录` / `读取 README.md` / `搜索 def` | 只读工具自动放行、带行号读取、截断提示 | [06](/core/tools) |
| `创建 hello.py` | default 模式下写操作弹出 **审批卡片**；状态机进入 `waiting_human`；执行前打影子 git 快照 | [17](/governance/permissions-hitl)、[18](/governance/workspace-sandbox) |
| ``运行 `python3 hello.py` `` | bash 命令风险分析、流式输出 | [17](/governance/permissions-hitl) |
| `并行检查` | 同一轮三个只读调用 **并发执行**，结果按原序回填 | [06](/core/tools) |
| `制定计划` | `todo_write` 外置计划、计划复述、Stop 验收 | [07](/core/context-engineering)、[08](/core/termination-budget) |
| `委托子任务` | 子代理在隔离会话里工作，只回传结构化契约 | [20](/multi-agent/subagents) |
| `等待 20 秒` | 可中断工具；点“停止”看 `running → cancelling → cancelled` | [12](/reliability/cancellation) |
| `发送通知` | EXTERNAL 级副作用、幂等键 | [14](/reliability/idempotency) |
| `压缩演示` | 水位上升 → 旧结果清理 → 摘要压缩 | [07](/core/context-engineering) |
| `循环测试` | 重复调用检测：先提醒，再以 `loop_detected` 结束 | [08](/core/termination-budget) |
| `错误演示` | 工具报错 → 错误作为结果回给模型 → 模型换策略 | [06](/core/tools) |

### 界面导览

```
┌─ 侧边栏 ──────────┬─ 会话页头：标题 · 运行状态 · 实时连接 · 权限模式菜单 · 暂停 / 停止 / 继续 / 重试 ─┬─ 检查器 ─┐
│ 新建会话           │                                                                          │ 概览     │
│ 搜索 / 命令面板 ⌘K │  对话：消息、思考过程、工具调用（点开看参数 / 实时输出 / 结果 / diff / 快照）、 │ 上下文   │
│ 运行记录           │        审批卡片、系统注入、压缩分隔线、运行小结                              │ 事件     │
│ 审批（待处理计数） │                                                                          │ 历史     │
│ 工具               ├─ 输入区：空闲时“发送”；运行中切换为“插话 / 排队” ─────────────────────────┤          │
│ 最近会话 · 外观    │                                                                          │          │
└───────────────────┴──────────────────────────────────────────────────────────────────────────┴──────────┘
```

侧边栏的三个页面面向“管理”：**运行记录** 汇总所有会话的运行（状态筛选、搜索、按 token / 费用 / 耗时排序）；
**审批** 是跨会话的待审批收件箱，可以直接在这里批准、修改参数后批准或拒绝；**工具** 列出全部工具及其副作用等级。
`⌘K`（Windows / Linux 上是 `Ctrl+K`）打开命令面板，可以搜索会话、切换权限模式与主题；`/` 聚焦输入框，`⌘\` 显示或隐藏检查器。

会话页右侧检查器的四个页签是学习时最有用的部分：

- **概览**：当前 Run 的 ID、尝试次数、版本号、Worker 与心跳；预算条（轮次 / 工具调用 / token / 费用）；
  每一次 CAS 状态迁移及其原因与发起人（页面打开前就结束的 Run 会从轨迹里补加载）；可展开的状态机全图；
  本会话的历史运行（继续 / 重试、确定性回放、下载轨迹 JSON）；
- **上下文**：此刻的上下文水位与两条阈值线（旧结果清理、摘要压缩）、system / tools / messages 的 token 构成、
  “模型此刻看到的每一条消息”及其 token 占比、当前生效的压缩摘要、冻结的系统提示词；
- **事件**：原始事件流（带 `seq`），可按类型筛选、搜索。点“模拟断线重连”会主动断开 SSE，两秒后凭 `Last-Event-ID` 续传——
  观察 seq 是否连续、有没有重复；
- **历史**：只追加的会话分支树，点“从此分支”把 head 移到任意历史位置，下一条输入就会长出一个新分支；
  下方是工作区快照列表，可以回滚到任意快照（会先确认）。

## 接入真实模型

provider 由 `--provider`（或配置 `provider`）决定，`auto` 模式按环境变量自动选择：

::: code-group

```bash [Anthropic]
export ANTHROPIC_API_KEY=sk-ant-...
flywhl serve -w playground                       # 默认模型 claude-opus-5-5
flywhl serve -w playground --model claude-sonnet-5-5
```

```bash [DeepSeek]
export DEEPSEEK_API_KEY=sk-...
flywhl serve -w playground --provider openai --model deepseek-chat
```

```bash [OpenAI 兼容服务]
export OPENAI_API_KEY=sk-...
export FLYWHL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1   # 例：通义千问
flywhl serve -w playground --provider openai --model qwen-plus
```

```bash [本地 Ollama]
export OPENAI_API_KEY=ollama
export OPENAI_BASE_URL=http://localhost:11434/v1
flywhl chat -w playground --provider openai --model qwen3.5:9b-mlx
```

:::

::: tip 对 Anthropic 模型默认开启的两项设置
- **前缀缓存**：请求带顶层 `cache_control`，并且系统提示与工具集在会话内冻结，重复的前缀按缓存价计费；
- **server-side fallback**：对支持的模型发送 `fallbacks: "default"`（beta `server-side-fallback-2026-07-01`），
  请求被安全策略拒答时由服务端改用推荐的备用模型。不需要时构造 `AnthropicProvider(server_fallback=False)`。
详见 [05 消息模型与模型接入层](/core/messages-and-models)。
:::

## 命令行

```bash
flywhl chat -w playground               # 终端交互；Ctrl-C 取消当前运行，连按两次退出
flywhl run "总结 README" --json         # 一次性执行，stdout 输出 JSONL 事件流
flywhl run "修复 TODO 并跑测试" --yes   # 需要审批的操作自动放行（只在可信环境中使用）
flywhl runs                             # 列出运行：状态、attempt、version、token
flywhl resume <run_id>                  # 恢复暂停 / 中断的运行
flywhl retry <run_id>                   # 重试失败的运行：从历史继续，attempt + 1
flywhl recover                          # 接管心跳过期（进程崩溃）的运行并自动恢复
flywhl replay <run_id>                  # 确定性回放：不调用模型、不执行副作用
flywhl replay <run_id> --fork-at 2      # 前 2 次模型调用用录制结果，之后换成当前模型
flywhl trace <run_id> -o trace.json     # 导出轨迹（含 OTel GenAI 风格 span）
flywhl tools                            # 工具列表与副作用等级
```

`--json` 模式下 harness 处于“无人值守”状态：需要审批的操作默认 **拒绝**（加 `--yes` 改为放行）。
这一点很重要——在 CI 里卡住等一个永远不会来的审批，比直接失败更糟。

## 配置文件

在工作区（或启动目录）放一个 `flywhl.toml`，所有字段都有默认值，未知字段会直接报错（防止拼写错误被静默忽略）：

```toml
provider = "openai"
model = "deepseek-chat"
base_url = "https://api.deepseek.com"
max_tokens = 8192
verify_command = "python3 -m pytest -q"      # Stop 验收：测试不过不让结束

[budget]
max_turns = 60
max_cost_usd = 0.5
max_wall_seconds = 1800

[context]
compact_threshold = 0.8
keep_recent_tokens = 12000
tool_result_max_tokens = 6000

[permissions]
mode = "default"                             # default | accept_edits | plan | bypass
rules = ["allow:bash(git status*)", "allow:bash(python3 -m pytest*)", "deny:bash(git push*)"]

[[hooks]]                                    # 进程外 hook：协议与 Claude Code 兼容
event = "pre_tool_use"
matcher = "bash"
command = "python3 scripts/guard.py"
fail_closed = true

[mcp_servers.demo]                           # MCP 服务器：工具名会加上 mcp__demo__ 前缀
command = "python3"
args = ["examples/mcp_echo_server.py"]
```

加载顺序：默认值 → `flywhl.toml` → 环境变量（`FLYWHL_PROVIDER`、`FLYWHL_MODEL`、`FLYWHL_BASE_URL`、
`FLYWHL_PERMISSION_MODE`……）→ 命令行参数。完整字段见 `src/flywhl_harness/config.py`。

## 作为库使用

```python
import asyncio
from flywhl_harness import Harness, HarnessConfig

async def main():
    async with Harness(HarnessConfig.load(workspace="playground")) as h:
        result = await h.run("列出目录", on_event=lambda ev: print(ev.seq, ev.type))
        print(result.status, result.text)

asyncio.run(main())
```

`Harness` 是门面：`create_session` / `start_run` / `subscribe` / `cancel` / `pause` / `resume` / `retry` / `steer` /
`resolve_approval` / `recover`。`flywhl serve` 启动时先接管一次崩溃遗留的运行，之后周期性检查；
停止服务时，正在执行的运行会在下一个安全点 **暂停**，下次启动自动继续。事件消费者（CLI、Web、你自己的代码）都只依赖事件流，这是 [03 架构总览](/guide/architecture)
要展开的第一个设计原则。

## 跑测试与示例

```bash
.venv/bin/python -m pytest -q                       # 156 个测试，约 15 秒
.venv/bin/python examples/02_crash_resume.py        # 真实 kill -9 后恢复，副作用恰好一次
```

`examples/` 下的 10 个示例全部使用脚本化模型，不需要 API Key，每个示例开头的注释说明了它演示什么、该观察什么。

## 数据都存在哪

```
playground/.flywhl/
├── harness.db            # SQLite（WAL）：sessions / entries / runs / steps / events / approvals / blobs
├── external_notify.db    # 演示用的“外部系统”：send_notification 写到这里（按幂等键去重）
└── shadow.git/           # 影子 git 仓库：写操作前的工作区快照，不影响你自己的 .git
```

想从头再来，删掉 `.flywhl/` 即可。

## 常见问题

**端口被占用？** `flywhl serve --port 9000`。

**想在局域网或反向代理后面访问？** `flywhl serve --host 0.0.0.0` 会自动生成访问令牌，并打印带 `?token=…` 的链接，用它打开一次即可；
通过自定义域名访问时用 `--allow-host 域名` 加入 Host 白名单。服务默认只接受 `127.0.0.1` / `localhost` 的 Host，并拒绝跨站的写请求——
这个服务能批准审批、切换权限模式，等于能在本机执行命令，原因见 [19 安全](/governance/security)。

**Web UI 需要联网吗？** 不需要。页面只使用系统字体与内置图标，不请求任何外部资源，离线与内网环境下显示一致。

**真实模型不调用工具？** 先确认模型本身支持 function calling；本地小模型可以先用 `flywhl run "用 ls 看看当前目录" --provider openai --model ...`
验证工具调用是否正常。

**审批卡片不出现，命令直接执行了？** 检查会话页头的权限模式是否是“完全放行”（`bypass`），以及 `flywhl.toml` 里是否有匹配的 `allow:` 规则。
