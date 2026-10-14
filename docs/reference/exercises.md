# 动手练习：从零实现 8 个组件

读懂一个机制最可靠的办法，是关掉参考实现、自己写一遍，再用测试去“攻击”它。下面 8 道练习按依赖顺序排列，
每道题都给出 **需求 → 关键点 → 验收标准 → 参考实现**。建议每道题限时 30–45 分钟，写完后对照 flywhl-harness 的实现找差异。

::: tip 练习方式
1. 新建一个空目录，只允许使用标准库（第 8 题可以用 FastAPI）；
2. 模型接口自己假设：`async def llm(messages, tools) -> AssistantMessage`，用一个脚本化的假模型驱动；
3. 先写验收测试，再写实现；
4. 写完后阅读对应章节的“设计取舍与常见坑”，看看自己踩中了几个。
:::

## 1. 最小 Agent Loop：工具调用 + 预算 + 终止

**需求**：给定 `llm(messages, tools)` 与工具注册表，实现循环：调用模型 → 执行工具 → 回填结果 → 直到模型给出最终答案；
限制最大轮次与 token 预算；检测重复调用。

**关键点**

- assistant 的每个 `tool_call` 都必须有一条 `tool_call_id` 相同的结果，顺序与调用一致；
- 工具异常、未知工具名、参数不合 schema 都转成 **模型可读的错误结果**，而不是抛出；
- 预算耗尽时让模型做一次 **无工具的总结性收尾**（保留工具定义，`tool_choice="none"`），并以 `budget_exhausted` 结束；
- 重复调用检测用 `(工具名, 规范化参数)` 的签名，结合结果哈希区分“原地打转”与“合理轮询”。

**验收**：非法工具名、参数不合 schema、工具抛异常、预算耗尽、死循环五种情况各有明确行为；结束状态如实区分。

**参考**：`agent/loop.py` · `AgentLoop._body()`、`agent/executor.py` · `_prepare()`、`agent/budget.py` · `LoopGuard`。
测试 `tests/test_loop.py::test_five_failure_modes_become_readable_errors`、`test_budget_exhausted_wraps_up`、`test_loop_guard_stops_dead_loop`。
讲解：[04 Agent Loop](/core/agent-loop)、[08 终止条件、预算与循环守卫](/core/termination-budget)。

## 2. 检查点与恢复：崩溃后不重复副作用

**需求**：在第 1 题的基础上，每一步把状态追加写入 SQLite；进程被杀后重启，从最后一个完成的步骤继续，
已执行过的副作用工具不能再执行一次。

**关键点**

- 消息落盘之后再对外发事件；工具结果与“步骤完成”标记在 **同一个事务** 里写入；
- 副作用工具执行 **前** 写 intent（started），执行后写 result；
- 恢复时：找到最后一条 assistant 消息里没有结果的调用——没有 intent 的直接执行；有 intent 无 result 的，
  **凭幂等键向外部系统对账**，查到就补写结果，查不到再按“只读/幂等可重试、否则上报结果未知”处理；
- 幂等键必须跨重启稳定（例如 `run_id:call_id`），不能是随机数或时间戳；
- 多个 Worker 时，接管要钉住扫描时看到的版本（`UPDATE … WHERE version=?`）：扫描之后别人已经接管的，这次接管必须失败；
  续租也要是条件更新，旧 Worker 的心跳不能把新主人的租约改回来。

**验收**：在工具执行中途 `kill -9`，重启后最终结果一致、外部副作用只发生一次；
两个 Worker 基于同一次过期观察先后接管，只有一个成功。

**参考**：`runtime/store.py` · `step_started()` / `step_finished()`、`agent/executor.py` · `_run_one()` 的 recovering 分支、
`runtime/manager.py` · `recover()`、`tools/demo.py` · `SendNotificationTool.reconcile()`。
测试 `tests/test_recovery.py::test_kill_dash_9_then_recover`、`tests/test_lifecycle.py::test_takeover_based_on_stale_observation_is_fenced`；示例 `examples/02_crash_resume.py`。
讲解：[11 检查点与崩溃恢复](/reliability/checkpoint-recovery)、[14 幂等与副作用](/reliability/idempotency)。

## 3. 协作式取消与超时

**需求**：支持外部发起取消；正在执行的工具按“可中断 / 不可中断”区分处理；整体与单工具都有超时。

**关键点**

- 取消是请求，在安全点生效：用 `asyncio.Event` 实现令牌，提供 `guard(awaitable)` 与 `iterate(async_iter)` 两个组合子；
- 可中断工具直接取消任务（子进程要杀整个进程组）；不可中断工具给宽限期，超时则记为“结果未知”并保留幂等键；
- `cancelling` 是中间状态；状态迁移用 CAS，取消与完成同时到达时只有一个生效；
- 没有结果的调用补一条“已取消”的合成结果，会话才能继续。

**验收**：取消请求后一个安全点内停止；状态迁移合法；审计中能看到取消原因与发起人。

**参考**：`cancel.py` · `CancelToken`、`agent/executor.py` · `_race()`、`runtime/manager.py` · `cancel()` / `_finish()`。
测试 `tests/test_cancel.py`（7 个用例）；示例 `examples/03_cancel_semantics.py`。讲解：[12 取消语义](/reliability/cancellation)。

## 4. 带退避与抖动的重试器 + 错误分类

**需求**：实现 `retry(fn, policy)`：区分可重试（限流、超时、5xx）与不可重试（参数错误、权限不足）；指数退避 + 全抖动；
尊重服务端返回的 `Retry-After`。

**关键点**

- 全抖动：`delay = uniform(0, min(cap, base · 2^attempt))`，有 `Retry-After` 时以它为下限；
- 截止时间沿调用链传递：剩余时间不足以再试一次就不试；
- 熔断器打开时不再重试；重试预算限制重试量占请求量的比例；
- 全链路只有一层负责重试（SDK 自带的重试要关掉）。

**验收**：分类正确；退避时间落在理论区间内；熔断器 closed → open → half_open → closed 的迁移正确。

**参考**：`runtime/retry.py`、`llm/resilient.py`、`llm/base.py` · `classify_http_error()`。
测试 `tests/test_runtime.py::test_full_jitter_bounds_and_retry_after`、`test_circuit_breaker_states`、`test_retry_budget`。
讲解：[13 重试、熔断与降级](/reliability/retry-fallback)。

## 5. DAG 任务调度器：并发上限 + 失败策略

**需求**：给定子任务及依赖，按拓扑顺序并行执行；全局并发上限 N、每个租户并发上限 M；某任务失败时支持
“快速失败 / 跳过下游 / 继续”三种策略；最后合并结果。

**关键点**：入度表 + 就绪队列；信号量控制并发；环检测（Kahn 算法剩余节点即环上节点）；
合并结果时记录每个子任务的来源、尝试次数与耗时；按实际耗时求关键路径。

**验收**：峰值并发不超过上限；失败只影响该策略应影响的任务；存在环时报错并指出涉及的任务。

**参考**：`runtime/scheduler.py`。测试 `tests/test_runtime.py::test_dag_order_concurrency_and_policies`、`test_dag_retries_and_cycle`；
示例 `examples/07_swarm_dag.py`。讲解：[21 DAG 调度与 Swarm](/multi-agent/swarm-dag)。

## 6. 上下文压缩器

**需求**：当消息列表的 token 数超过阈值时，保留最近 k 个 token 的原始消息与所有未完成的工具调用对，
把更早的历史压缩为摘要；超大工具结果外置为句柄。

**关键点**

- 切点只能落在用户消息（其次 assistant 消息）上，**绝不能落在工具结果上**——否则结果会和它的调用分离；
- 摘要保留“目标、约束、已完成/进行中/受阻、关键决策、文件与 ID、下一步”；原始任务原文用代码确定性保留；
- 已有摘要时做增量更新，避免“摘要的摘要”；
- 只在回合边界（全部调用已配对）时压缩。

**验收**：任意保留阈值下，压缩后的视图中每个工具结果都能找到它的调用；摘要中包含原始任务原文。

**参考**：`agent/compaction.py` · `find_cut_index()`、`Compactor.compact()`、`runtime/session.py` · `SessionLog.context()`、
`agent/context.py` · `shape_tool_result()`。测试 `tests/test_context.py`（7 个用例）；示例 `examples/04_context_compaction.py`。
讲解：[07 上下文工程与压缩](/core/context-engineering)。

## 7. 幂等的指令下发服务

**需求**：云端向设备 / 本地 Agent 下发指令：幂等键去重、ACK 状态机（pending → sent → acked / failed / expired）、
超时重发、离线排队与 TTL；端侧幂等消费。

**关键点**：去重靠唯一约束；状态迁移是条件更新，终态不可变；重复与迟到的 ACK 被忽略；
端侧维护“已执行指令表”，重复投递时不再执行而是重发上次的 ACK。

**验收**：在丢包、重复、乱序的网络上，每条指令在端侧恰好执行一次；过期指令不再下发，迟到的 ACK 不能改变其状态。

**参考**：`runtime/dispatch.py`。测试 `tests/test_runtime.py::test_dispatch_exactly_once_effect_over_flaky_network`、`test_dispatch_ttl_expire`；
示例 `examples/09_edge_cloud.py`。讲解：[26 端云协同](/advanced/edge-cloud)。

## 8. SSE 流式接口 + 中途打断 + 断线续传

**需求**：用 FastAPI 实现 SSE 流式返回 Agent 输出；客户端断开时停止推送但不停止任务；支持断线续传（`Last-Event-ID`）；
支持用户中途停止或插话。

**关键点**

- 生成任务与连接解耦：生成写入事件日志，连接只是订阅者；
- 事件 `id` 单调递增；订阅时先注册实时队列、再从日志补发、最后按 seq 去重——不重不漏；
- 心跳注释防止代理断开空闲连接；慢客户端有界缓冲，溢出时让它从日志补齐；
- 停止走取消语义；插话排队到下一个安全点注入，不破坏工具调用配对。

**验收**：任意位置断开重连，客户端收到的事件 seq 连续、无重复；断开期间任务照常完成。

**参考**：`runtime/manager.py` · `EventHub`、`Harness.subscribe()`、`server/app.py` · `events()`、`web/app.js` · `connect()` / `onEvent()`。
测试 `tests/test_runtime.py::test_sse_resume_with_last_event_id`；Web UI“事件”页签的“模拟断线重连”。
讲解：[15 流式输出、插话与断线续传](/reliability/streaming)。

## 进阶挑战

- 把第 2 题的 SQLite 换成 PostgreSQL，用 `SELECT … FOR UPDATE SKIP LOCKED` 实现多 Worker 抢占任务，并用版本号做 fencing；
- 给第 6 题加一个“压缩质量探针”：压缩前从历史中抽取 5 个关键事实，压缩后让模型仅凭摘要回答，统计召回率；
- 用第 5 题的调度器 + 子代理（[20 子代理与回传契约](/multi-agent/subagents)）实现一个“多文件并行重构”，并用评测对比单 Agent 的成功率与成本。
