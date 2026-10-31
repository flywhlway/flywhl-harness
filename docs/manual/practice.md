# 实战手册

## 1. 安装与首次运行

```bash
uv venv && uv pip install -e ".[dev,anthropic]"   # 需要 Python 3.11+
.venv/bin/flywhl serve -w playground
```

终端会打印带令牌的地址 `http://127.0.0.1:8765/?token=…`，点开即可；令牌保存在 `~/.flywhl/server_token`，重启后链接不变。`-w` 指定工作区：所有文件操作都被限制在其中，会话库与快照存放在 `playground/.flywhl/`。

一个密钥都没配时自动使用演示模型 `flywhl-demo`。开始页的“演示场景”按剧本触发各种机制：说“创建 hello.py”看审批卡片与快照，“并行检查”看只读并发，“等待 20 秒”后点停止看取消，“压缩演示”看水位与压缩，“循环测试”看循环守卫。

## 2. 接入真实模型

```bash
cp .env.example .env     # 填一个密钥即可，例如 DEEPSEEK_API_KEY
flywhl doctor            # 对主模型、降级链和各供应商做“流式文本 + 工具往返”自检
```

`provider = "auto"` 时按 DeepSeek → Anthropic → OpenAI → 百炼 → Kimi → 智谱 → MiniMax → OpenRouter 的顺序选第一个有密钥的。doctor 方括号里的错误分类就是 harness 的处理方式：`rate_limit` 退避重试；`billing`、`not_found` 不原地重试，直接沿降级链换模型。

## 3. 项目配置

加载顺序：默认值 → `~/.flywhl/config.toml` → `flywhl.toml` → `FLYWHL_*` 环境变量 → 命令行参数。未知字段直接报错，拼写错误不会被静默忽略。

```toml
model = "deepseek:deepseek-flash"
fallback_models = ["dashscope:qwen3.8-flash"]   # 可以跨供应商
verify_command = "python3 -m pytest -q"         # 测试不过，模型就不能宣布完成

[budget]
max_turns = 40
max_cost_usd = 0.5

[permissions]
mode = "default"
rules = ["allow:bash(git status*)", "allow:bash(python3 -m pytest*)", "deny:bash(git push*)"]
```

`verify_command` 与 allow 规则属于高危设置，首次加载要确认信任，见[配置信任](./security#配置信任)。

选权限模式的经验：日常开发用 `default`；信任它改代码、但想盯住命令时用 `accept_edits`；只想先看方案用 `plan`；`bypass` 只留给一次性容器。审批时选“本会话内总是允许”，bash 按命令前两个词授权（如 `pytest -q`）；含内联代码、包装命令或变量赋值的，只授权这一条完整命令。

## 4. 用好 Web UI

- **侧栏**：会话（可重命名、归档、导出为 Markdown）、运行记录、跨会话的审批收件箱、工具列表，以及「环境与安全」页（`#/settings`）。
- **会话页**：工具调用可展开参数、实时输出、diff 与快照；代码块一键复制并带轻量高亮；运行中输入可选“插话”或“排队”，输入框里 ↑ / ↓ 翻阅历史。
- **审批**：有待审批时标签页标题显示计数，并发桌面通知（在「环境与安全」页开启）；按 `A` 跳到第一个待审批操作，`Enter` 批准；也可以改参数后再批准。
- **检查器**：概览看状态迁移与预算，上下文看水位与 token 构成，事件看带 `seq` 的原始流，历史看会话分支树并回滚快照。

## 5. 作为库嵌入

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

`on_event` 收到的就是 CLI 与 Web 消费的同一条事件流。

`@tool` 的几个参数决定 harness 怎样对待这个工具：

| 参数 | 影响 |
|---|---|
| `effect` | `READ` 可自动放行、可并发；`WRITE` / `EXTERNAL` 执行前打快照，并按权限模式审批 |
| `interruptible` | 取消时能否立即中止；设为 False 则获得宽限期，超时记为“结果未知” |
| `idempotent` | 同一幂等键重复执行是否安全；为 True 时崩溃恢复可以直接重跑 |
| `reconcile` | 恢复时凭幂等键查询外部系统，返回已经产生的结果，实现恰好一次 |

函数声明一个 `ctx` 参数就能拿到运行上下文。调用外部 API 时把 `ctx.idempotency_key` 透传给对方（例如 `Idempotency-Key` 请求头），由对方去重，是实现恰好一次最省事的办法。

## 6. 命令 Hook 与 MCP

```toml
[[hooks]]
event = "pre_tool_use"
matcher = "bash"
command = "python3 scripts/guard.py"

[mcp_servers.echo]
command = "python3"
args = ["examples/mcp_echo_server.py"]   # 工具名形如 mcp__echo__<工具>
```

```python
# scripts/guard.py：stdin 是 JSON（hook_event_name、tool_name、tool_input……），与 Claude Code 协议兼容
import json, sys

call = json.load(sys.stdin)
if "git push" in call["tool_input"].get("command", ""):
    print("推送前请先让用户审阅", file=sys.stderr)
    sys.exit(2)   # 2 = 阻断，stderr 作为原因回给模型；0 = 放行
```

## 7. 无人值守与 CI

```bash
flywhl run "修复失败的测试" --json --mode accept_edits
```

`--json` 向 stdout 输出 JSONL 事件流；此时无人审批，需要审批的操作默认拒绝——在 CI 里卡住等审批比直接失败更糟。要放行确定安全的操作，用 `--approve 'bash(npm test*)' --approve 'write(src/**)'` 列出规则，只有命中的自动批准；高危命令和读过不可信内容后的外部操作照样拒绝。`--yes` 放行一切，只在一次性容器里用。进程退出码只在 Run 以 `completed` 结束时为 0，配合 `verify_command` 就是“测试不过即失败”。

## 8. 排障

| 现象 | 处理 |
|---|---|
| 模型调不通 | `flywhl doctor` 看错误分类，确认降级链每一项都能接通 |
| 进程崩溃后 Run 停在 running | `flywhl recover` 接管心跳过期的 Run |
| Run 失败想接着做 | `flywhl retry <run_id>`：从已有历史继续，attempt + 1 |
| 想复盘或换模型对比 | `flywhl replay <run_id>`，加 `--fork-at N` 从第 N 次模型调用起改用当前模型 |
| 需要交给别人分析 | `flywhl trace <run_id> -o trace.json` 导出轨迹 |
| 文件被改坏 | 检查器“历史”页回滚到写操作前的快照 |

深入阅读：[02 快速上手](/guide/quickstart)、[22 轨迹、回放与评测](/multi-agent/trajectory-eval)、[30 扩展指南](/advanced/extending)。
