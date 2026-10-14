# 知识地图

把 Agent Harness 工程的知识点按八个维度展开，每个知识点都对应到 **讲解章节、源码位置与验证方式**。
可以把它当作复习清单：逐行确认自己能讲清“为什么”、能指出代码在哪、能跑出证据。

## Loop

| 知识点 | 章节 | 源码 | 验证 |
|---|---|---|---|
| 一轮的阶段：整形 → 模型 → 落盘 → 工具 → 守卫 → 续跑判定 | [04](/core/agent-loop) | `agent/loop.py` · `_body()` | `test_basic_tool_loop` |
| 为什么主循环用 async generator（拉取式、背压、清理、透传） | [04](/core/agent-loop) | `AgentLoop.run()` | Web UI 事件页签 |
| tool_call / tool_result 配对不变量与各修补点 | [04](/core/agent-loop)、[12](/reliability/cancellation) | `types.py` · `unpaired_tool_calls()`、`_pair_dangling()`、`prepare_history()` | `test_cancel_interrupts_interruptible_tool_within_one_step` |
| 插话（steer）与排队（follow-up）的注入时机；验收期间到达的输入不丢 | [04](/core/agent-loop)、[15](/reliability/streaming) | `agent/inbox.py`、`_drain_inbox()` | `test_steer_injected_at_safe_point`、`test_follow_up_runs_after_natural_stop`、`test_follow_up_arriving_during_stop_check_is_processed` |
| ReAct / Plan-and-Execute / Reflexion 在 harness 中的落点 | [01](/guide/what-is-harness) | `todo_write`、`DagScheduler`、Stop 验收 | `examples/07`、`test_stop_hook_blocks_then_gives_up_honestly` |
| Agent 与 Workflow 的取舍 | [01](/guide/what-is-harness) | — | — |
| 输出被 max_tokens 截断时工具调用整批判失败 | [04](/core/agent-loop) | `_body()` | `test_truncated_tool_calls_are_not_executed` |

## 模型接入

| 知识点 | 章节 | 源码 | 验证 |
|---|---|---|---|
| 统一消息模型、内容块、content 与 details 分离 | [05](/core/messages-and-models) | `types.py` | — |
| 流事件归一化、partial JSON 拼装 | [05](/core/messages-and-models) | `llm/base.py` · `MessageBuilder` | `test_recorded_tool_call_streams`（真实 SSE 夹具） |
| OpenAI 兼容与 Anthropic 的协议差异 | [05](/core/messages-and-models) | `llm/openai_compat.py`、`llm/anthropic.py` | `test_anthropic_protocol_agent_task`（live） |
| 供应商档案：思考内容的来源与回传、思考开关 | [05](/core/messages-and-models) | `llm/profiles.py` · `ProviderProfile` | `test_reasoning_echo_styles`、`flywhl doctor` |
| 错误分类：余额不足、模型不存在、上下文超长 | [05](/core/messages-and-models)、[13](/reliability/retry-fallback) | `classify_http_error()` | `test_recorded_errors_are_classified` |
| 跨供应商降级链 | [13](/reliability/retry-fallback) | `create_provider()`、`ResilientProvider` | `test_cross_provider_fallback_on_bad_key`（live） |
| Function Calling、结构化输出、校验与修复 | [05](/core/messages-and-models)、[06](/core/tools) | `tools/schema.py` · `validate()` | `test_schema_validation_and_coercion` |
| temperature 与确定性 | [05](/core/messages-and-models) | — | `examples/08`（回放） |
| 前缀缓存与“只追加” | [05](/core/messages-and-models)、[23](/multi-agent/observability-cost) | `prompts.py`（冻结）、`_inject_reminders()` | `test_prefix_stability_append_only` |
| thinking 签名与历史编辑 | [05](/core/messages-and-models)、[07](/core/context-engineering) | `llm/anthropic.py`、`SessionLog.context()` | `test_thinking_stripped_from_retained_segment` |
| refusal 与 server-side fallback | [05](/core/messages-and-models) | `llm/anthropic.py` | `test_anthropic_server_fallback_only_on_first_party` |
| 模型能力矩阵、usage 计量与成本 | [05](/core/messages-and-models)、[23](/multi-agent/observability-cost) | `llm/models.py` | `test_recorded_final_answers_and_usage`、`test_unpriced_model_warns_that_cost_budget_is_ineffective` |
| 多模型路由、降级链 | [13](/reliability/retry-fallback) | `llm/resilient.py` | `test_resilient_provider_retry_then_fallback` |

## 工具

| 知识点 | 章节 | 源码 | 验证 |
|---|---|---|---|
| 工具设计原则（ACI）：粒度、命名、schema、错误信息 | [06](/core/tools) | `tools/fs.py`、`tools/shell.py` | `examples/01` |
| 副作用分级与对应的并发/重试/审批/取消策略 | [06](/core/tools) | `tools/base.py` · `Effect` | `test_modes` |
| 一轮多个调用的安全并行与原序回填 | [06](/core/tools) | `executor.py` · `run_batch()` | `test_parallel_read_batch_preserves_order` |
| 所有失败都变成模型可读的错误 | [06](/core/tools) | `executor.py` · `_prepare()` / `_invoke()` | `test_five_failure_modes_become_readable_errors` |
| 超大结果：截断、外置句柄、按会话隔离 | [06](/core/tools)、[07](/core/context-engineering) | `context.py` · `shape_tool_result()`、`BlobStore` | `test_large_tool_result_is_externalized` |
| 工具数量膨胀与渐进披露、工具检索 | [27](/advanced/protocols) | `prompts.py` · `find_skills()` | — |

## 上下文

| 知识点 | 章节 | 源码 | 验证 |
|---|---|---|---|
| 上下文腐化、lost-in-the-middle、注意力预算 | [07](/core/context-engineering) | — | — |
| 从轻到重的上下文手段全景 | [07](/core/context-engineering) | `agent/context.py` | `examples/04` |
| 水位估算：真实 usage + 增量估算 | [07](/core/context-engineering) | `ContextManager.estimate()` | `test_estimate_uses_real_usage` |
| 旧结果清理与水位线 | [07](/core/context-engineering) | `apply_clearing()` | `test_clearing_happens_before_compaction` |
| 压缩的触发时机、切点规则、保留什么 | [07](/core/context-engineering) | `agent/compaction.py` | `test_cut_never_lands_on_tool_result`、`test_auto_compaction_in_run` |
| 增量摘要、确定性保留原始任务与文件清单 | [07](/core/context-engineering) | `Compactor.compact()` | `test_auto_compaction_in_run` |
| 上下文超长错误的处理 | [07](/core/context-engineering)、[13](/reliability/retry-fallback) | `_ContextOverflow` | `test_context_overflow_forces_compaction` |
| 计划复述与 system-reminder | [07](/core/context-engineering) | `todo_reminder()`、`_inject_reminders()` | Web UI“制定计划” |
| 子代理作为上下文隔离手段 | [20](/multi-agent/subagents) | `run_subagent()` | `test_subagent_contract` |
| 记忆的分层（工作记忆 / 外置记忆 / 长期记忆） | [07](/core/context-engineering) | `todo_write`、文件 | — |

## 状态与生命周期

| 知识点 | 章节 | 源码 | 验证 |
|---|---|---|---|
| Session / Entry / Run / Step / Event 四层模型 | [09](/reliability/state-persistence) | `runtime/store.py` | — |
| 会话树与分支 | [09](/reliability/state-persistence) | `runtime/session.py` | Web UI 检查器“历史”页签 |
| 存储分层：关系库 / 列存 / 对象存储；outbox | [09](/reliability/state-persistence) | `BlobStore` | — |
| Run 状态机：状态、迁移表、语义区分 | [10](/reliability/run-lifecycle) | `runtime/state.py` | `test_state_machine_cas` |
| CAS 迁移与并发竞态（审批 vs 取消、取消 vs 完成） | [10](/reliability/run-lifecycle) | `cas_run()`、`_finish()` | `test_cancel_after_completion_loses_race` |
| 离线迁移同样留下审计事件 | [10](/reliability/run-lifecycle) | `_offline_transition()`、`append_event_next()` | `test_offline_transitions_are_audited` |
| 失败重试：从历史继续而不是从头再来 | [10](/reliability/run-lifecycle)、[11](/reliability/checkpoint-recovery) | `Harness.retry()` | `test_retry_failed_run_continues_from_history` |
| 快照式 vs 事件溯源式持久化 | [11](/reliability/checkpoint-recovery) | entries + steps + events | — |
| 预写 intent、结果未知与对账 | [11](/reliability/checkpoint-recovery)、[14](/reliability/idempotency) | `_run_one()` recovering 分支 | `test_kill_dash_9_then_recover` |
| 心跳租约、接管与 fencing；基于旧观察的接管必须失败 | [11](/reliability/checkpoint-recovery) | `recover()`、`transition(expect_version=)`、条件续租 `heartbeat()` | `test_recover_is_fenced`、`test_takeover_based_on_stale_observation_is_fenced`、`test_heartbeat_does_not_steal_lease` |
| 周期性接管、queued 孤儿、关闭服务时暂停 | [11](/reliability/checkpoint-recovery) | `recovery_loop()`、`aclose()` | `test_queued_orphan_is_recovered`、`test_shutdown_pauses_then_restart_resumes`、`test_user_pause_is_not_auto_resumed` |
| 暂停恢复与环境漂移说明 | [11](/reliability/checkpoint-recovery) | `_resume_note()` | `test_pause_and_resume_with_drift_note` |
| 非确定性的记录与重放 | [11](/reliability/checkpoint-recovery)、[22](/multi-agent/trajectory-eval) | `ReplayProvider` | `test_deterministic_replay` |
| 协作式取消、令牌传播、按副作用分级处理在途工具 | [12](/reliability/cancellation) | `cancel.py`、`_race()` | `tests/test_cancel.py` |
| 外部调用超时 = 结果未知 | [12](/reliability/cancellation)、[14](/reliability/idempotency) | `_race()` | `test_external_timeout_is_unknown_not_plain_timeout` |
| 抢占式终止与协作式取消在 journal 上的区别 | [11](/reliability/checkpoint-recovery)、[12](/reliability/cancellation) | `run_batch()` · `abrupt` | `test_in_process_crash_then_recover` |
| 补偿（Saga） | [12](/reliability/cancellation)、[14](/reliability/idempotency) | — | — |
| 重试分类、全抖动、Retry-After、重试预算、熔断（探测名额必须归还） | [13](/reliability/retry-fallback) | `runtime/retry.py` | `test_full_jitter_bounds_and_retry_after`、`test_circuit_breaker_probe_is_released_or_expires` 等 |
| 只一层负责重试；流式重试丢弃半截输出 | [13](/reliability/retry-fallback) | `ResilientProvider` | `test_loop_retries_model_and_reports` |
| 限流、背压、舱壁 | [13](/reliability/retry-fallback) | — | — |
| 幂等键、去重表、条件更新、状态机守卫；同键不同内容即冲突 | [14](/reliability/idempotency) | `FakeNotifyService`、`cas_run()` | `examples/02`、`test_fake_notify_rejects_same_key_different_payload` |
| “恰好一次”的真相 | [14](/reliability/idempotency) | — | `examples/09` |
| Saga / TCC / Outbox 选型 | [14](/reliability/idempotency) | — | — |
| 流式链路、Run 与连接解耦、Last-Event-ID 续传 | [15](/reliability/streaming) | `EventHub`、`subscribe()`、`server/app.py` | `test_sse_resume_with_last_event_id` |
| 停止 / 插话 / 排队三种打断语义 | [15](/reliability/streaming) | `inbox.py`、`cancel()` | Web UI |
| 持久执行引擎（Temporal）映射与双层编排 | [25](/advanced/durable-execution) | — | — |

## 策略与执行环境

| 知识点 | 章节 | 源码 | 验证 |
|---|---|---|---|
| Hooks 挂载点、决策组合、fail-open / fail-closed | [16](/governance/hooks) | `policy/hooks.py` | `test_hook_deny_and_rewrite`、`test_hook_fail_closed_on_timeout` |
| 决策取值归一化（`block` = deny、未知取值告警）；超时的命令 hook 必须被杀掉 | [16](/governance/hooks) | `normalize_decision()`、`CommandHook` | `test_pre_tool_block_is_deny`、`test_unknown_hook_decision_warns_instead_of_crashing`、`test_command_hook_is_killed_on_timeout` |
| 进程外 hook 协议（与 Claude Code 兼容） | [16](/governance/hooks) | `CommandHook` | `test_command_hook_protocol` |
| Stop hook 与“完成是产物的属性” | [08](/core/termination-budget)、[16](/governance/hooks) | `_stop_gaps()` | `test_stop_hook_blocks_then_gives_up_honestly` |
| 企业策略分层注入 | [16](/governance/hooks) | `Hook.source`、`MANAGED_BASELINE` | — |
| 权限模式、规则优先级、复合命令 | [17](/governance/permissions-hitl) | `policy/permissions.py` | `test_rule_priority_deny_over_allow`、`test_compound_command_cannot_piggyback` |
| 危险命令识别（为什么不能只用正则）：包装命令、`sh -c` 递归、工作区边界 | [17](/governance/permissions-hitl) | `policy/bash_analyzer.py` | `test_bash_analyzer`、`test_analyzer_sees_through_wrappers`、`test_outside_read_asks_and_reason_explains` |
| 灾难性命令在任何模式下都拒绝 | [17](/governance/permissions-hitl) | `catastrophic` | `test_catastrophic_is_denied_in_every_mode` |
| 命令替换不能借 allow 规则搭便车 | [17](/governance/permissions-hitl) | `opaque`、`_SUBST` | `test_allow_rule_cannot_cover_command_substitution` |
| 本地 Web 服务的访问控制（DNS 重绑定、CSRF、令牌） | [19](/governance/security) | `server/app.py` · `guard` | `test_server_rejects_dns_rebinding_and_cross_site_writes`、`test_server_token_flow` |
| 污点感知与提示注入（网络输出、跨暂停持久、子代理上传） | [17](/governance/permissions-hitl)、[19](/governance/security) | `tainted`、`untrusted_for()` | `test_taint_escalates_side_effects`、`test_network_output_taints_session_and_persists` |
| 伪造的 system-reminder 标签 | [19](/governance/security) | `shape_tool_result()` | `test_reminder_tags_in_tool_output_are_escaped` |
| 人审：持久化、超时、TOCTOU、变更预览、改参后重跑守卫 | [17](/governance/permissions-hitl) | `policy/approvals.py`、`Tool.preview()` | `test_approval_edit_args_revalidated`、`test_edited_args_rerun_pre_tool_hooks`、`test_approval_request_carries_diff_preview` |
| 终止条件与分层预算 | [08](/core/termination-budget) | `agent/budget.py` | `test_budget_exhausted_wraps_up` |
| 死循环与无进展检测 | [08](/core/termination-budget) | `LoopGuard` | `test_loop_guard_stops_dead_loop` |
| 过早停止的根因与系统性解法 | [08](/core/termination-budget)、[22](/multi-agent/trajectory-eval) | Stop 验收、`premature_stop_rate` | `examples/08` |
| Workspace 抽象与生命周期 | [18](/governance/workspace-sandbox) | `runtime/workspace.py` | — |
| 快照与回滚、快照技术路线对比 | [18](/governance/workspace-sandbox) | `ShadowGit` | Web UI 检查器“历史”页签、`test_shadow_repo_is_per_workspace` |
| 多 Agent 并行改同一仓库：worktree 与合并 | [18](/governance/workspace-sandbox) | — | — |
| 沙箱运行时选型、冷启动与预热池 | [18](/governance/workspace-sandbox) | — | — |
| 网络出口管控与凭据代持 | [18](/governance/workspace-sandbox)、[19](/governance/security) | `tools/shell.py` · `_clean_env()` | — |
| 浏览器自动化隔离 | [18](/governance/workspace-sandbox) | — | — |
| 提示注入纵深防御、最小权限、供应链、OWASP Agentic Top 10、红队 | [19](/governance/security) | `policy/` | — |
| 端云协同：大脑/双手、接管、离线队列、对账、产物交付 | [26](/advanced/edge-cloud) | `runtime/dispatch.py` | `test_dispatch_exactly_once_effect_over_flaky_network`、`test_edge_dedupes_duplicate_arriving_mid_execution` |

## 多 Agent 与度量

| 知识点 | 章节 | 源码 | 验证 |
|---|---|---|---|
| 多 Agent 模式与何时不该用 | [20](/multi-agent/subagents) | — | — |
| 子代理任务简报与回传契约、违约处理 | [20](/multi-agent/subagents) | `agent/subagent.py` | `test_subagent_contract`、`examples/06` |
| 共享上下文：黑板、产物库、只传引用 | [20](/multi-agent/subagents) | `blob_scope` | — |
| 任务拆解与 DAG 调度、并发配额、失败策略、关键路径 | [21](/multi-agent/swarm-dag) | `runtime/scheduler.py` | `examples/07`、`test_dag_continue_policy_passes_failure_and_cancel_is_immediate` |
| 结果合并与冲突裁决 | [21](/multi-agent/swarm-dag) | — | — |
| 调度公平与防饥饿（先租户、后全局名额）、弹性伸缩 | [21](/multi-agent/swarm-dag) | `DagScheduler.run()` | `test_dag_tenant_limit_does_not_block_other_tenants` |
| 轨迹数据模型、上下文指纹 | [22](/multi-agent/trajectory-eval) | `observe/trace.py`、`context_fingerprint()` | `flywhl trace`、`test_fingerprint_sees_tool_description_change` |
| 确定性回放与反事实回放 | [22](/multi-agent/trajectory-eval) | `observe/replay.py` | `examples/08` |
| 评测体系、LLM-as-Judge 偏差、统计显著性 | [22](/multi-agent/trajectory-eval) | `observe/evals.py` | `test_wilson_and_mcnemar` |
| 非确定系统的测试、回归门禁、灰度与影子流量 | [22](/multi-agent/trajectory-eval) | `ScriptedProvider` | `tests/` |
| OTel GenAI 语义约定与 trace 传播 | [23](/multi-agent/observability-cost) | `to_otel_spans()` | `examples/08` |
| 成本优化组合拳 | [23](/multi-agent/observability-cost) | — | — |
| 事故排查：长任务卡在 running 且成本暴涨 | [23](/multi-agent/observability-cost) | `runs` / `events` 表 | `flywhl runs` |

## 进阶

| 知识点 | 章节 | 源码 | 验证 |
|---|---|---|---|
| 长时任务：初始化 agent、功能清单、进度文件、每会话一个单元 | [24](/advanced/long-running) | `examples/10_long_running.py` | 运行示例 |
| MCP 与 Function Calling 的关系、A2A、Skills、AGENTS.md | [27](/advanced/protocols) | `tools/mcp.py`、`prompts.py` | `test_mcp_tools` |
| 可删减的 harness：能力补偿层 vs 治理层、消融评测 | [28](/advanced/evolving-harness) | 配置开关 | `observe/evals.py` |
| 极简 harness 的取舍（pi） | [29](/advanced/pi-comparison) | — | — |
| 扩展：工具、provider、hook、存储、远程沙箱 | [30](/advanced/extending) | — | `tests/conftest.py` |

## 三张随身清单

**端云设计六问**——任何端云协同方案先过一遍：

1. 事实源在哪一侧？（通常是云端事件日志）
2. 指令 / 工具调用的幂等键是什么？
3. 断网期间本地能做什么、不能做什么？
4. 重连后如何对账：序号、版本、冲突规则？
5. 谁可以接管执行？接管时迁移哪些状态？
6. 产物如何交付：内容寻址、分片续传、完整性校验？

**时序假设四问**——对每一个跨系统的时序假设问：如果它 **晚到、重复、乱序、永不到达**，系统会怎样？

**Harness 八件套速记**：Loop（整形 → 模型 → 工具 → 钩子 → 续跑判定）· 工具（schema、可操作错误、幂等、风险分级）·
上下文（裁剪、压缩、外置句柄、子代理隔离）· 状态（Session / Run / Step / Event，只追加）·
生命周期（暂停、恢复、取消、重试、崩溃恢复）· 策略（hooks、权限、人审、预算）·
执行环境（Workspace、沙箱、端云）· 度量（轨迹、回放、评测、成本）。
