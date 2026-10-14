# 源码地图

`src/flywhl_harness/` 约 10K 行 Python（其中大量是中文注释）加约 3K 行前端（原生 JS 与 CSS，无构建步骤）。下面给出推荐的阅读顺序，
以及每个文件“读它时该带着什么问题”。

## 推荐阅读顺序

| 顺序 | 文件 | 带着这个问题读 | 配套章节 |
|---|---|---|---|
| 1 | `types.py` | 为什么 system 不在消息列表里？`details` 为什么不送给模型？ | [05](/core/messages-and-models) |
| 2 | `events.py` | 一个 UI 需要哪些事件才能完整还原一次 Run？ | [03](/guide/architecture) |
| 3 | `agent/loop.py` | 一轮有哪些阶段？Loop 如何保证一定以明确状态结束？ | [04](/core/agent-loop) |
| 4 | `agent/executor.py` | 一个工具调用从“想调用”到“结果落盘”经过哪几道关？ | [06](/core/tools) |
| 5 | `cancel.py` | `guard` 和 `iterate` 如何让任意等待都能被取消打断？ | [12](/reliability/cancellation) |
| 6 | `runtime/store.py` | 哪些写入必须在同一个事务里？CAS 写在哪？ | [09](/reliability/state-persistence) |
| 7 | `runtime/state.py` | 哪些状态迁移是非法的？为什么终态不可变？ | [10](/reliability/run-lifecycle) |
| 8 | `runtime/manager.py` | Run 与 HTTP 连接如何解耦？崩溃后谁来接管？ | [11](/reliability/checkpoint-recovery)、[15](/reliability/streaming) |
| 9 | `agent/context.py` + `agent/compaction.py` | 水位怎么估？切点为什么不能落在工具结果上？ | [07](/core/context-engineering) |
| 10 | `policy/permissions.py` + `policy/bash_analyzer.py` | 规则、模式、命令分析、污点如何叠加成一个决定？ | [17](/governance/permissions-hitl) |
| 11 | `policy/hooks.py` | hook 为什么只能收紧不能放宽？ | [16](/governance/hooks) |
| 12 | `llm/base.py` + `llm/anthropic.py` | 不同 provider 的流式协议如何归一化？ | [05](/core/messages-and-models) |
| 13 | `llm/resilient.py` + `runtime/retry.py` | 流式输出已经推给 UI 之后还能重试吗？ | [13](/reliability/retry-fallback) |
| 14 | `agent/subagent.py` | 子代理凭什么不污染父上下文？ | [20](/multi-agent/subagents) |
| 15 | `observe/` | 回放时如何发现“harness 行为变了”？ | [22](/multi-agent/trajectory-eval) |

## 逐文件说明

### 顶层

| 文件 | 行数量级 | 内容 |
|---|---|---|
| `types.py` | 230 | `Message`、`TextBlock`、`ThinkingBlock`、`ToolCall`、`Usage`、`StopReason`、`MsgKind`、`unpaired_tool_calls()` |
| `events.py` | 80 | 事件类型常量 `E`、`Event`、`EPHEMERAL_EVENTS` |
| `cancel.py` | 130 | `CancelToken`（`cancel` / `child` / `guard` / `iterate` / `sleep`）、`RunCancelled`、`RunPaused` |
| `config.py` | 380 | `HarnessConfig` 及子配置；`load()`：默认值 → 用户配置 → 项目配置 → 环境变量 → 参数，未知键报错；项目配置信任（`split_untrusted`、`trusted.json`） |
| `prompts.py` | 150 | 系统提示、`find_project_context()`（AGENTS.md）、`find_skills()`（渐进披露）、压缩模板、`reminder()` |
| `_utils.py` | 170 | `new_id`、`canonical_json`、`estimate_tokens`、截断函数、`extract_json_block` |
| `cli.py` | 450 | `flywhl` 命令、`TerminalRenderer`、终端审批、`.env` 白名单与项目配置信任确认 |

### agent/ —— 大脑

| 文件 | 内容 |
|---|---|
| `loop.py` | `AgentLoop`：`run()` 外层兜底、`_body()` 主循环、`_call_model()` 流事件翻译、`_maybe_compact()`、`_inject_reminders()`、`_wrap_up()`、`_stop_gaps()` |
| `executor.py` | `ToolExecutor`：`run_batch()` 分批并发与原序落盘、`_prepare()` 校验、`_run_one()` 权限/hooks/审批（变更预览、改参后重跑守卫）/intent/执行/外置/污点、`_race()` 与取消超时赛跑（外部调用超时记为结果未知） |
| `context.py` | `ContextManager`：`estimate()`、`thresholds()`、`shape_tool_result()`（外置大结果、转义伪造的 system-reminder 标签）、`apply_clearing()`、`todo_reminder()` |
| `compaction.py` | `find_cut_index()`、`serialize_transcript()`、`extract_file_ops()`、`Compactor` |
| `budget.py` | `Budget`（软/硬阈值、分层 `child()`）、`LoopGuard` |
| `inbox.py` | 插话（steer）与排队（follow-up） |
| `subagent.py` | `TaskTool`、`CONTRACT_SCHEMA`、`check_contract()` |
| `run_context.py` | `RunContext`：一次 Run 的全部依赖与可变状态 |

### llm/ —— 模型接入

| 文件 | 内容 |
|---|---|
| `base.py` | `ModelRequest`、`StreamEvent`、`ErrorKind`、`ProviderError`、`classify_http_error()`、`MessageBuilder`、`Provider`、`prepare_history()` |
| `models.py` | `ModelInfo` 能力矩阵、`MODELS` 价格表、`compute_cost()` |
| `profiles.py` | `ProviderProfile` 供应商档案（地址、密钥变量、思考内容的来源与回传方式、思考开关）、`<供应商>:<模型>` 解析 `resolve()` |
| `openai_compat.py` | 用 httpx 直读 Chat Completions SSE（DeepSeek / 百炼 / Kimi / 智谱 / MiniMax / OpenRouter / Ollama…）；思考回传、`<think>` 拆分、流完整性 |
| `doctor.py` | `flywhl doctor` 接入自检：流式文本 + 工具往返 |
| `anthropic.py` | 基于官方 SDK 的原始流事件；tool_result 合并、thinking 回传、缓存、refusal、server-side fallback |
| `mock.py` | `ScriptedProvider`（测试与示例）、`DemoProvider`（无 Key 演示）、`deterministic_summary()` |
| `replay.py` | `ReplayProvider`、`context_fingerprint()` |
| `resilient.py` | `ResilientProvider`：重试 + 熔断 + 降级链 |
| `__init__.py` | `create_provider()`（可跨供应商的降级链）、`detect_provider()` |

### tools/ —— 双手

| 文件 | 内容 |
|---|---|
| `base.py` | `Effect`、`ToolResult`、`ToolContext`、`Tool`（`effect_for` / `interruptible_for` / `untrusted_for` / `preview` / `reconcile`）、`FunctionTool`、`@tool` |
| `schema.py` | 迷你 JSON Schema 校验器、`schema_from_signature()` |
| `registry.py` | 顺序稳定的注册表、`subset()` |
| `fs.py` | `read` / `write` / `edit` / `ls` / `grep`，过期读取保护、严格 UTF-8、原子写、审批 diff 预览 |
| `shell.py` | `bash`：进程组、两段式终止、流式输出、保尾截断、密钥环境变量剥离 |
| `extra.py` | `todo_write`、`read_ref`、`search_ref`、`latest_todos()` |
| `demo.py` | `FakeNotifyService`（同键不同内容报 `IdempotencyConflict`）、`send_notification`、`sleep` |
| `mcp.py` | 最小 MCP stdio 客户端与 `MCPTool`（注解的保守默认值、环境隔离、`notifications/cancelled`） |

### policy/ —— 策略

| 文件 | 内容 |
|---|---|
| `permissions.py` | `Mode`、`Rule`、`PermissionPolicy.evaluate()`、`MANAGED_BASELINE`、`grant_for()` |
| `bash_analyzer.py` | `analyze()`：词法切分、剥包装命令、`sh -c` / `eval` 递归分析、工作区边界、`catastrophic` 与 `network` 标记；无法静态分析的结构一律保守 |
| `hooks.py` | `HookEvent`、`HookInput`、`HookOutput`、`normalize_decision()`、`Hook`、`HookBus`、`CommandHook` |
| `approvals.py` | `ApprovalBroker`：持久化审批、超时、CAS 决定一次 |

### runtime/ —— 运行时

| 文件 | 内容 |
|---|---|
| `manager.py` | `Harness` 门面、`EventHub`、生命周期控制（`pause` / `resume` / `retry` / `cancel` / `aclose`）、`recover()` 与 `recovery_loop()`、`_offline_transition()`、`subscribe()`、`run_subagent()` |
| `store.py` | SQLite 表结构、事务、CAS、条件续租、`append_event_next()`、`BlobStore` |
| `session.py` | `SessionLog`：会话树、分支、上下文视图、`render_summary()` |
| `state.py` | `RunStatus`、`TRANSITIONS`、`RunStateMachine`（`expect` / `expect_version`） |
| `workspace.py` | `Workspace`（路径约束、清单漂移）、`ShadowGit`（快照/对比/回滚，按工作区隔离） |
| `retry.py` | `RetryPolicy`、`CircuitBreaker`（探测名额 `release()` 与过期）、`RetryBudget`、`retry_async()` |
| `scheduler.py` | `DagScheduler`、`TaskSpec`、`UpstreamFailed`、`critical_path()` |
| `dispatch.py` | `CommandDispatcher`、`EdgeExecutor`（已执行表 + 在途去重）、`FlakyTransport` |

### observe/、server/、web/

| 文件 | 内容 |
|---|---|
| `observe/trace.py` | `export_trajectory()`、`to_otel_spans()` |
| `observe/replay.py` | `replay_run()`、`RecordedTool`、`HybridProvider` |
| `observe/evals.py` | `EvalCase`、`run_eval()`、`wilson_interval()`、`mcnemar_exact()` |
| `server/app.py` | FastAPI：会话/Run（含暂停、恢复、重试）/审批/上下文检查器/快照 REST；SSE（`Last-Event-ID`、心跳、断开检测）；lifespan 中启动周期性恢复；访问控制中间件（Host 白名单、跨站写请求检查、可选令牌） |
| `web/app.js` | 控制台入口：路由（深链接 `#/s/<会话>`、`#/runs`、`#/approvals`、`#/tools`）、侧边栏、命令面板、全局快捷键、审批计数轮询 |
| `web/views/session.js` | 会话工作区：SSE 订阅与按 seq 去重、断线续传、rAF 合并的流式渲染、贴底滚动、运行控制、权限模式 |
| `web/views/timeline.js` | 时间线渲染器：消息、思考过程、工具调用卡片、审批卡片（风险、命令分析、diff 预览、可编辑参数）、压缩分隔线、运行小结 |
| `web/views/inspector.js` | 检查器：概览（状态流转、预算、状态机）、上下文、事件、历史（分支与快照） |
| `web/views/pages.js` | 运行记录、审批收件箱、工具目录 |
| `web/lib/` | `api`（请求与错误）、`ui`（提示、对话框、菜单、命令面板，含焦点管理）、`meta`（状态与副作用的文案）、`icons`、`dom` |
| `web/md.js` | 先转义再渲染的极简 Markdown |

## 测试与示例对照

| 测试文件 | 守护的机制 |
|---|---|
| `tests/test_loop.py` | Loop 主干、五种错误、并行批与原序、预算收尾、循环检测、截断调用、Stop 验收、插话/排队、前缀稳定、结果外置 |
| `tests/test_cancel.py` | 流式中途取消、可中断/不可中断工具、审批中取消、取消与完成竞争、暂停恢复与漂移说明、外部调用超时记为结果未知 |
| `tests/test_recovery.py` | 进程内崩溃恢复、接管的 fencing、**真实 kill -9**、对账结果先于恢复说明 |
| `tests/test_lifecycle.py` | 失败重试、关闭时暂停与重启续跑、用户暂停不自动恢复、queued 孤儿、基于旧观察的接管被拒、续租不抢租约、离线迁移审计、验收期间的追加输入、原子写 |
| `tests/test_context.py` | 切点规则、自动压缩、保留段去 thinking、只追加模型不清理、先清理后压缩、usage 估算 |
| `tests/test_policy.py` | 命令分析、规则优先级、复合命令、四种模式、污点、会话授权、hooks、命令 hook 协议、状态机 CAS、审批改参 |
| `tests/test_runtime.py` | 退避与熔断（含探测名额归还）、降级、上下文超长、DAG（租户公平、失败信息下传、即时取消）、端云指令（含在途去重）、幂等键冲突、子代理契约、SSE 续传、MCP、回放与指纹、统计、schema |
| `tests/test_providers.py` | 用录制的真实 SSE 与错误响应离线验证各家差异：并行工具调用拼接、三种思考来源、思考回传方式、思考开关翻译、流不完整与流中错误、错误分类、模型写法解析、跨供应商降级链、`.env` 加载 |
| `tests/live/` | 真实模型端到端（`pytest -m live`，默认不跑）：接入层、Agent 机制、运行时与 Web API，见 `tests/live/README.md` |
| `tests/test_security.py` | hook 决策归一化、命令 hook 超时被杀、命令分析的绕过写法、灾难性命令全模式拒绝、工作区外读取、命令替换不被 allow 规则放行、网络输出污点、伪造提醒标签、改参重跑守卫、审批预览、MCP 注解默认值、影子仓库隔离、Web 服务访问控制 |

示例 `examples/01`–`10` 的说明见 [02 快速上手](/guide/quickstart) 与仓库 README。
