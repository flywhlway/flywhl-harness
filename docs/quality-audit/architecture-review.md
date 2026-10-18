# 架构与代码审计

## 系统是什么

单进程 Python 包，不是微服务。一个 `Harness` 同时持有 SQLite、工具注册表、权限策略和模型供应商。入口有三个：库 API、`flywhl` CLI、FastAPI（REST + SSE + `web/` 静态控制台）。

```
CLI / Web UI
    → Harness（runtime/manager.py）
        → AgentLoop（agent/loop.py）
            → Provider（llm/）
            → Executor（agent/executor.py）
                → PermissionPolicy + Hooks + ApprovalBroker
                → Tool.execute
                    → Workspace / bash / MCP / 演示用外部通知
        → Store（SQLite WAL）
```

工作区默认就是启动目录。数据目录默认是工作区下的 `.flywhl/`（`harness.db`、影子 git、审批记录）。没有租户、没有用户表、没有服务间认证。访问控制只存在于 HTTP 层：Host 白名单、Origin 检查、可选访问令牌。

## 模块边界

| 包 | 职责 | 边界评价 |
|---|---|---|
| `agent/` | 主循环、执行器、上下文、压缩、预算、子代理 | 循环与执行器分开，权限判定集中在执行器，边界清楚 |
| `llm/` | 供应商档案、OpenAI 兼容、Anthropic、演示模型、重试降级 | `resolve()` 把模型名和供应商探测叠在一起，`model = "mock"` 会被探测结果盖掉（QA-006） |
| `tools/` | 文件、bash、MCP、沙箱原型 | 文件工具执行时不再查凭证清单，完全依赖权限层的字符串匹配 |
| `policy/` | 模式、规则、bash 分析、敏感路径、hook、审批 | 安全不变量的实现都在这里，也是本次缺陷的集中处 |
| `runtime/` | 状态机、CAS、会话树、崩溃接管、调度、影子 git | 状态迁移有白名单，离线测试和干净环境下的 kill -9 演示能对上 |
| `server/` + `web/` | HTTP 与无构建步骤的控制台 | 服务层没有第二道业务权限；拿到 HTTP 写权限就能改模式、批审批、发起运行 |
| `observe/` | 轨迹、回放、评测 | 确定性回放不执行副作用；分叉时 EXTERNAL 改 dry-run，WRITE 仍会在副本里执行 |

核心依赖只有 httpx。FastAPI/uvicorn 只服务 Web。Anthropic SDK 是可选额外依赖。这个收敛是真实的，`pyproject.toml` 与导入关系一致。

## 数据与权限

会话消息、运行、步骤、事件、审批都进 SQLite。事件只追加。运行状态用版本号做 CAS。写文件走临时文件 + `fsync` + `os.replace`。外部通知演示工具按幂等键去重。这些机制有对应离线测试，干净环境下 `examples/11_kill9_cli_recover.py` 七条不变量全部成立。

权限判定顺序与代码注释一致：灾难性命令和凭证线索先拒绝，然后 deny → ask → allow，最后才是模式默认值。plan 模式会把非只读从 allow/ask 压成 deny。污点会话会把副作用的 allow 升成 ask。审批人改过的参数会再跑 schema、权限和 PreToolUse hook。

这套顺序在“分析器看懂了命令”时是成立的。本次失败都发生在分析器没把真实执行看成同一件事的地方：

- bash 按换行切命令，分析器只按 `; && || | &` 切段（QA-001）
- 文件系统大小写不敏感，`fnmatchcase` 和正则敏感（QA-002）
- 环境剥离发生在子进程，`ps` 看的是父进程（QA-003）
- `-lc` 不被当成 `-c`，`find -delete` 不被当成删家目录（QA-005）
- “本会话始终允许”对 bash 取前两个词再加 `*`（QA-005）

HTTP 守卫在 `server/app.py` 的中间件里。Host 不在白名单返回 403，这在 `Host: evil.example` 上复现通过。Origin 为 `https://evil.example` 和 `null` 也是 403。Origin 只取主机名，回环地址的任意端口都算通过（QA-004）。令牌用 `hmac.compare_digest`，cookie 是 HttpOnly + SameSite=Strict，没有 Secure。查询串里的 token 对所有路径都有效，只有 `/` 会 303 到去掉 token 的地址。

## 已经核对、没有打成缺陷的部分

- SQL 使用参数占位，没有看到字符串拼接查询。
- 静态目录用 Starlette `StaticFiles`。`/static/../pyproject.toml` 返回 404，没有读到仓库文件。
- Markdown 先转义再生成标签，链接只接受 `https?:`。本次没有做成 XSS。
- 控制台把动态文本放进 `innerHTML` 之前走 `esc()`。抽查会话标题、工具结果、diff，都经过转义。
- 启动目录的 `.env` 有白名单，`flywhl.toml` 里会执行命令或放宽审批的字段要信任后才生效。这次没有发现白名单被绕开。
- 小写路径 `.ssh/config` 在 default、plan、bypass、accept_edits 下都是 deny。
- 子进程会去掉名字里含 `KEY` / `TOKEN` / `SECRET` / `PASSWORD` / `CREDENTIAL` / `AUTH` 的变量。探针里的 `DEEPSEEK_API_KEY` 没有出现在 `echo` 输出中。
- 简单形式的 `rm -rf ~` 仍被标成灾难性。现有 `tests/test_security.py` 覆盖了包装命令、`sh -c` 和花括号。缺口在换行、合并短选项和 `find -delete`。

## 生产形态上的结构限制

这些不是本次新发现的逻辑错误，但决定了它不能直接当多租户服务：

- 一个进程、一个工作区根、一份 SQLite。没有租户隔离。
- 监听 `0.0.0.0` 时 Host 白名单改成 `*`，只剩共享令牌。令牌是进程级的，不是用户级的。
- 沙箱默认 `off`。`macos` 档是 Seatbelt 原型：从 `(allow default)` 收紧文件读写，不限制网络、进程查看和钥匙串（`tools/sandbox.py` 自己写了这个范围）。
- 命令 hook 默认 `fail_closed=False`。超时只警告，现有测试把这一点固定下来了。
- `PRAGMA synchronous=NORMAL`。注释写明挡得住进程崩溃，挡不住断电。
- 数据库文件在这次 UAT 里是 `0644`。同一台机器上的其他用户能读会话和工具输出。

## 可维护性

源码约 1.1 万行 Python，Web 约 3 千行，测试约 3.5 千行，注释密度高，模块按机制拆开，和 README 的目录表一致。重复实现主要是“同一安全结论要在 bash 字符串和文件路径上各做一遍”，敏感路径已经收进 `policy/sensitive.py`，但匹配规则没有跟文件系统的大小写语义对齐。

技术债里需要先处理的不是抽象层级，而是上面三个 P0：分析器和真正的 shell、真实路径不是同一个判定。
