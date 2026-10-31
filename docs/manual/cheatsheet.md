# 速查表

## 命令

| 命令 | 用途 |
|---|---|
| `flywhl chat` | 终端交互；Ctrl-C 取消当前运行，连按两次退出 |
| `flywhl run "<任务>" [--json] [--yes]` | 一次性执行；`completed` 时退出码为 0 |
| `flywhl serve [--host] [--port] [--token \| --no-token] [--allow-host]` | Web UI，默认 `127.0.0.1:8765` |
| `flywhl sessions` / `flywhl runs [--session]` | 列出会话 / 运行 |
| `flywhl resume <run_id>` / `flywhl retry <run_id>` | 恢复暂停或中断的运行 / 重试失败的运行 |
| `flywhl recover` | 接管心跳过期的运行 |
| `flywhl replay <run_id> [--fork-at N]` | 确定性回放 / 反事实分叉 |
| `flywhl trace <run_id> -o trace.json` | 导出轨迹 |
| `flywhl export <会话ID> [-o 文件] [--injected]` | 会话导出为 Markdown；`--injected` 附带注入给模型的提醒 |
| `flywhl tools` / `flywhl doctor [模型…]` | 工具与副作用等级 / 模型接入自检 |

通用参数：`-w` 工作区，`-c` 可信配置文件，`--trust` 信任项目配置，`--model`、`--provider`，`--env-file`，`--mode`，`-y` 自动批准，`--sandbox macos`，`--thinking` 显示思考过程。

终端内命令：`/new` 新会话，`/mode <模式>` 切换权限，`/compact` 手动压缩，`/tree` 会话树，`/branch <entry_id>` 切换分支，`/exit` 退出。

## 文件与位置

| 路径 | 内容 |
|---|---|
| `flywhl.toml` | 项目配置，高危项需信任 |
| `~/.flywhl/config.toml` | 用户配置，视为可信 |
| `~/.flywhl/trusted.json` | 已信任的配置文件（路径 + SHA-256） |
| `~/.flywhl/server_token` | Web 访问令牌（0600） |
| `<工作区>/.flywhl/` | 会话库、审批记录、影子 git 快照 |
| `.env` | 各家密钥与 `LLM_PRIMARY_MODEL`、`LLM_FALLBACK_MODEL` |

## 关键配置与默认值

| 配置 | 默认 |
|---|---|
| `model` / `provider` | `deepseek:deepseek-flash` / `auto` |
| `max_tokens` / `max_retries` | 8192 / 4 |
| `tool_timeout` / `cancel_grace_seconds` | 120 秒 / 5 秒 |
| `parallel_tool_limit` / `subagent_max_depth` | 8 / 2 |
| `verify_todos` / `max_stop_blocks` | true / 3 |
| `budget.max_turns` / `max_tool_calls` / `soft_ratio` | 40 / 200 / 0.8 |
| `context.clear_threshold` / `compact_threshold` | 0.55 / 0.8 |
| `context.reserve_tokens` / `keep_recent_tokens` / `tool_result_max_tokens` | 8192 / 12000 / 6000 |
| `loop_guard.warn_repeats` / `stop_repeats` / `max_consecutive_errors` | 3 / 5 / 6 |
| `permissions.mode` / `on_ask` / `approval_timeout` | `default` / `wait` / 600 秒 |
| `permissions.sandbox` / `strip_secret_env` | `off` / true |

## 规则语法

`决策:工具(glob)`，决策取 `allow`、`ask`、`deny`；省略括号表示该工具的全部调用。bash 匹配命令文本，文件工具匹配路径。

```toml
rules = ["allow:bash(git status*)", "ask:write(src/**)", "deny:read(**/.ssh/**)", "deny:send_notification"]
```

## Hook 事件

`session_start` · `user_prompt_submit` · `pre_tool_use` · `post_tool_use` · `stop` · `subagent_stop` · `pre_compact` · `run_end`

## Web 快捷键

`⌘K` / `Ctrl+K` 命令面板 · `/` 聚焦输入框 · `⌘\` 显示或隐藏检查器 · `Enter` 发送，`Shift+Enter` 换行 · `↑` / `↓` 输入历史 · `A` 定位第一个待审批操作（再按 `Enter` 批准） · `Esc` 关闭弹层
