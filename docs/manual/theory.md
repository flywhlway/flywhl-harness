# Harness 理论精要

## 模型之外的一切

模型是一个无状态函数：输入消息列表，输出下一条消息，其中可能带工具调用。它不会自己循环，不记得上次运行到哪，也无权决定能不能删文件。这些能力全部由 Harness 提供，所以同一个模型配上不同的 Harness，表现可以天差地别。

<svg viewBox="0 0 640 220" role="img" aria-label="Agent 由 Model 与包裹它的 Harness 组成，Harness 有八个维度" style="display:block;width:100%;max-width:640px;margin:20px auto;font-size:14px">
<rect x="2" y="2" width="636" height="216" rx="16" style="fill:none;stroke:var(--vp-c-brand-1);stroke-dasharray:6 4"/>
<text x="18" y="24" style="fill:var(--vp-c-brand-1);font-weight:600">Harness</text>
<path d="M320 86L110 66M320 86L250 66M320 86L390 66M320 86L530 66M320 134L110 154M320 134L250 154M320 134L390 154M320 134L530 154" style="stroke:var(--vp-c-divider);fill:none"/>
<g style="fill:var(--vp-c-bg-soft);stroke:var(--vp-c-divider)">
<rect x="55" y="34" width="110" height="32" rx="8"/><rect x="195" y="34" width="110" height="32" rx="8"/><rect x="335" y="34" width="110" height="32" rx="8"/><rect x="475" y="34" width="110" height="32" rx="8"/>
<rect x="55" y="154" width="110" height="32" rx="8"/><rect x="195" y="154" width="110" height="32" rx="8"/><rect x="335" y="154" width="110" height="32" rx="8"/><rect x="475" y="154" width="110" height="32" rx="8"/>
</g>
<g text-anchor="middle" style="fill:var(--vp-c-text-1)">
<text x="110" y="55">循环</text><text x="250" y="55">工具</text><text x="390" y="55">上下文</text><text x="530" y="55">状态</text>
<text x="110" y="175">生命周期</text><text x="250" y="175">策略</text><text x="390" y="175">执行环境</text><text x="530" y="175">度量</text>
</g>
<rect x="230" y="86" width="180" height="48" rx="24" style="fill:var(--vp-c-brand-soft);stroke:var(--vp-c-brand-1)"/>
<text x="320" y="107" text-anchor="middle" style="fill:var(--vp-c-text-1);font-weight:600">Model</text>
<text x="320" y="125" text-anchor="middle" style="fill:var(--vp-c-text-2);font-size:12px">消息 → 下一条消息</text>
</svg>

| 维度 | 要回答的问题 | 本项目的做法 |
|---|---|---|
| 循环 | 何时调用模型，何时停下 | 异步生成器驱动回合；模型想结束时先过 Stop 验收 |
| 工具 | 模型怎样作用于世界 | JSON Schema 校验；副作用分 READ / WRITE / EXTERNAL 三级；只读调用并发 |
| 上下文 | 有限窗口里放什么 | 水位监控、旧结果清理、增量压缩、大结果外置为句柄 |
| 状态 | 什么必须落盘 | 会话、Run、Step、事件全部写入 SQLite，崩溃后可重建 |
| 生命周期 | 怎样暂停、取消、恢复 | 14 个状态的状态机，CAS 版本号，心跳租约 |
| 策略 | 允许它做什么 | 权限模式与规则、Hooks、人工审批、污点追踪 |
| 执行环境 | 在哪里做，做坏了怎么办 | 工作区路径约束、影子 git 快照、macOS 沙箱 |
| 度量 | 做得好不好 | 轨迹导出、确定性回放、带置信区间的评测 |

## 四条不变量

全部代码都在守这四条；排查问题时先问“哪一条被破坏了”。

1. **事件是唯一出口。** Loop 只产出事件，从不直接操作界面。CLI、Web、库消费同一条事件流，新增一种客户端不需要改核心。
2. **先落盘，再发事件。** 客户端看到的任何变化，进程被 `kill -9` 之后都能从库里找回；反过来，没落盘的东西绝不会先被看到。
3. **历史只追加。** 已经发给模型的消息不再改写：前缀缓存持续命中，思考签名保持有效，回放与原始执行逐字一致。压缩只改变“模型这一轮看到什么”，不删除历史条目；回到旧位置继续对话，会长出一条新分支。
4. **失败显式化。** 工具报错、超时、被拒、被取消，都变成与调用配对的 `tool_result` 回给模型，缺失时补一条合成结果；Run 以如实的终态结束，验收没通过就是 `incomplete`，不会伪装成 `completed`。

## 每个机制对应一个模型短板

| 短板 | 机制 |
|---|---|
| 长任务里忘记目标 | `todo_write` 外置计划；隔几轮没更新，就把未完成项以 system-reminder 复述到上下文末尾 |
| 原地兜圈子 | 循环守卫：重复调用先警告、再终止 |
| 没做完就宣布完成 | Stop 验收：未完成的待办与 `verify_command` 失败都会阻止结束 |
| 被超长输出淹没 | 超过阈值的结果外置，模型按需检索 |
| 偶发的接口错误 | 退避重试、熔断、跨供应商降级 |

模型变强后，有些短板会消失。所以这些机制都做成可开关、可度量（如 `verify_todos`、`clear_old_tool_results`、`snapshots`），去留由评测数据决定，而不是凭感觉继续堆脚手架。

## 几处关键取舍

| 选择 | 理由 |
|---|---|
| Loop 写成异步生成器 | 每个 `yield` 都是天然的暂停与取消点；调用方用 `async for` 消费事件，测试可以逐个事件断言 |
| 状态放 SQLite（WAL） | 单文件、零运维、有事务；状态迁移的 CAS 只需一条 `UPDATE … WHERE` |
| 策略在执行层判定 | 写在提示词里的约束会被注入覆盖，执行层的判定不会；plan 模式也只在执行层拒绝，工具定义不变，前缀缓存不失效 |
| 只读并发、写入串行 | 写入的先后会改变结果，只读不会 |
| 隔离分层设防 | 路径约束挡顺手的越界，命令分析挡常见变形，刻意混淆交给内核沙箱；没有哪一层假装自己是最后一道 |

深入阅读：[01 什么是 Harness](/guide/what-is-harness)、[04 Agent Loop](/core/agent-loop)、[28 可删减的 Harness](/advanced/evolving-harness)。
