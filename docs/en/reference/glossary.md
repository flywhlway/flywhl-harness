# Glossary

Terms as these docs use them, with the Chinese term used in the full tutorial and the code comments.

## Basics

| Term | Chinese | Meaning |
|---|---|---|
| **Harness** | Harness | The runtime that drives a model through a task: loop, tools, context, state, lifecycle, policy, execution environment and measurement. `Agent = Model + Harness` |
| **Run** | Run | One task execution, from one user input to an explicit end state. It can be paused, resumed and retried |
| **Session** | 会话 | The long-lived container shared by runs: workspace, frozen system prompt and tool set, and an append-only entry tree |
| **Turn** | 回合 | One model call plus the tool execution after it. Codex defines it more broadly, as everything from one user input to one assistant reply |
| **Step** | Step | The journal record of one tool call (intent, then result), used to tell what already happened during recovery |
| **Event** | 事件 | The fine-grained fact stream inside a run, with a monotonic `seq`, used for streaming, resumption, audit and replay |
| **Entry** | 条目 | A node in the session tree: a message or a compaction summary. Append-only; branching only moves `head` |
| **Safe point** | 安全点 | A place in the main loop where responding to cancel or pause is safe: phase boundaries and after tool results are persisted |
| **Provider** | 供应商 | A model vendor or gateway, addressed as `<provider>:<model>` |

## Messages and models

| Term | Chinese | Meaning |
|---|---|---|
| **tool_call / tool_result pairing** | 配对 | Every tool call must have a result with the same `tool_call_id`, or the next request is rejected by the API |
| **content / details** | content / details | The part of a tool result the model sees, and the part kept for the UI and audit (borrowed from pi) |
| **stop_reason** | stop_reason | Why an assistant message ended: `end_turn`, `tool_use`, `max_tokens` (arguments may be cut off), `aborted`, `error`, `refusal` |
| **Thinking signature** | thinking 签名 | Claude's thinking blocks carry a signature that must be sent back unchanged when continuing with the same model |
| **Prompt caching** | 前缀缓存 | The part of a request byte-identical to an earlier prefix is billed at the cache rate. Depends on append-only history and deterministic serialization |
| **Append-only** | 只追加 | History is never modified; changes are expressed by appending new messages |
| **Server-side fallback** | server-side fallback | Anthropic's `fallbacks` parameter: when a request is refused by a safety policy, the server retries with a fallback model in the same call |
| **ModelInfo** | 能力矩阵 | A model's context window, maximum output, prices and behavior traits (`llm/models.py`) |

## Tools and context

| Term | Chinese | Meaning |
|---|---|---|
| **ACI** | ACI | Agent-Computer Interface: designing tools as an interface for the model |
| **Effect (side-effect level)** | 副作用等级 | `read`, `write` (reversible) or `external` (irreversible or visible outside). Sets concurrency, retry, approval and cancellation behavior |
| **Blob handle** | 外置句柄 | An oversized tool result is written to content-addressed storage; the context keeps a preview and a handle, read on demand with `read_ref` and `search_ref` |
| **Context usage** | 水位 | The share of the context window in use. Past one threshold stale results are cleared; past a higher one the history is compacted |
| **Clearing watermark** | 水位线 | A forward-only marker for result clearing, so the cached prefix changes only when the marker moves |
| **Compaction** | 压缩 | Rewriting earlier history as a structured summary. Lossy, and the last resort of context management |
| **Cut point** | 切点 | Where compaction starts keeping original messages. It must never fall on a tool result |
| **system-reminder** | system-reminder | A note the harness injects for the model (budget, loops, plan recitation), stored as a marked user message |
| **Recitation** | 计划复述 | Periodically appending the unfinished plan to the end of the context, to limit goal drift in long tasks |
| **Progressive disclosure** | 渐进披露 | Give only a list first (skills, tool summaries) and load the full text when needed |

## Reliability

| Term | Chinese | Meaning |
|---|---|---|
| **CAS transition** | CAS 迁移 | `UPDATE … WHERE status=? AND version=?`. Zero rows affected means a concurrent conflict |
| **Cooperative cancellation** | 协作式取消 | Cancellation is a request and each layer stops at its own safe point, as opposed to preemptive `task.cancel()` |
| **Unknown outcome** | 结果未知 | It can't be determined whether an external operation took effect: it was still running when the cancel grace period ended, a non-idempotent call timed out, or the process crashed mid-call. Needs reconciliation |
| **Reconciliation** | 对账 | Asking the external system for the real result of an operation, using its idempotency key |
| **Idempotency key** | 幂等键 | A stable key for "the same operation". Here, `run_id:call_id` |
| **Heartbeat / lease** | 心跳 / 租约 | A worker periodically refreshes `heartbeat_at`, conditional on still holding the lease. An expired lease counts as a crash and another worker takes over |
| **Takeover** | 接管 | Another worker picking up a run whose lease expired |
| **Fencing** | fencing | Stopping an old holder from writing after it lost the lease. Here, CAS on `version`: takeover pins the version it saw, and the old worker cancels itself when it finds the lease has moved |
| **Offline transition** | 离线迁移 | A state change with no live event stream (create, recover, offline cancel, crash takeover). Still written as a `status` event |
| **Full jitter** | 全抖动 | Backoff of `uniform(0, min(cap, base·2^n))`, which scatters retries from many clients |
| **Circuit breaker** | 熔断器 | closed → open (fail fast) → half_open (one probe) → closed. A probe that is cancelled or inconclusive must give its slot back |
| **Fallback chain** | 降级链 | An ordered list of models to try when the current one fails |
| **Steer / follow-up** | 插话 / 排队 | Input sent during a run: a steer is injected at the next safe point, a follow-up after the current work ends |
| **Saga / compensation** | Saga / 补偿 | Each step of a multi-step process has a compensating action, run in reverse order on failure |
| **Outbox** | Outbox | State changes and pending events are written in one transaction, then delivered asynchronously |

## Policy and execution

| Term | Chinese | Meaning |
|---|---|---|
| **Hook** | Hook | Deterministic code at a lifecycle point that can intercept, modify, audit or block |
| **Fail-open / fail-closed** | fail-open / fail-closed | Whether a hook's own failure lets the action through or blocks it. Security hooks should fail closed |
| **Permission mode** | 权限模式 | `default`, `accept_edits`, `plan`, `bypass` |
| **Rule** | 规则 | `allow\|ask\|deny:tool(glob)`, evaluated deny → ask → allow; the first match wins |
| **Human approval** | 人审 | A person approving, editing or rejecting an action, durably, so it survives a pause or a restart |
| **Taint** | 污点 | After a session reads untrusted content, side-effecting operations escalate to human confirmation. Stored in session metadata, so it survives pause and resume |
| **Bash analysis** | 命令分析 | Static risk analysis of a command. A filter for the common case, and explicitly not a security boundary |
| **Catastrophic command** | 灾难性命令 | Deleting the root or home directory, formatting a disk, writing to a block device, a fork bomb. Denied in every mode, including `bypass` |
| **TOCTOU** | TOCTOU | Time of check versus time of use. Arguments edited by an approver must be validated and judged again |
| **Workspace** | 工作区 | Where the agent works: files, dependencies, execution environment and metadata |
| **OS sandbox** | 系统沙箱 | Kernel-enforced confinement: `sandbox-exec` on macOS, bubblewrap on Linux |
| **Egress allowlist** | 出站白名单 | Hosts commands may reach through the local egress proxy. Empty by default |
| **Human gate (`outside_sandbox`)** | 人工门 | The separate approval needed to run one command outside the sandbox. No mode, rule or session grant can pass it |
| **Shadow git** | 影子 git | A separate repository with `GIT_DIR` in the data directory and its work tree on the workspace, used for snapshots and rollback |
| **Brain / hands / session** | 大脑 / 双手 / 会话 | Model plus harness (stateless), tools and sandbox (replaceable), and the append-only event log (source of truth) |

## Multi-agent and measurement

| Term | Chinese | Meaning |
|---|---|---|
| **Subagent** | 子代理 | An agent that works on a subtask in isolated context and returns only a structured result |
| **Return contract** | 回传契约 | The JSON a subagent must return: status, summary, findings, artifacts, open_questions |
| **Swarm** | Swarm | Several agents working in parallel on one large task. Modeled here by the DAG scheduler |
| **Critical path** | 关键路径 | The longest dependency chain in a DAG, which sets the total duration |
| **Trajectory** | 轨迹 | The full record of a run: input, each nondeterministic result, control-plane events and context fingerprints |
| **Context fingerprint** | 上下文指纹 | A hash of what the model saw at each call (system prompt, tool names, descriptions and schemas, messages), used to locate where a replay diverged |
| **Deterministic / counterfactual replay** | 确定性回放 / 反事实回放 | Replaying exactly from the recording, or switching to a new model or prompt from step k |
| **Premature stop rate** | 过早停止率 | The share of runs that claimed completion but failed the artifact check |
| **Wilson interval** | Wilson 区间 | A binomial confidence interval that behaves well on small samples |
| **McNemar test** | McNemar 检验 | A paired test comparing two versions on the same cases, using only the cases where they disagree |
| **OTel GenAI conventions** | OTel GenAI 语义约定 | OpenTelemetry's span and attribute conventions for generative AI (`gen_ai.*`) |
