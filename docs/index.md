---
layout: home

hero:
  name: "Agent = Model + Harness"
  text: "从零手写一个 Agent Harness"
  tagline: 模型负责“想”，harness 负责其余的一切——循环、工具、上下文、状态、生命周期、策略、执行环境与度量。这套教程用一份可运行的 Python 实现（flywhl-harness）把每个机制讲到源码级。
  image:
    src: /favicon.svg
    alt: flywhl
  actions:
    - theme: brand
      text: 开始阅读
      link: /guide/what-is-harness
    - theme: alt
      text: 三分钟跑起来
      link: /guide/quickstart
    - theme: alt
      text: 知识地图
      link: /reference/knowledge-map

features:
  - icon: ⟳
    title: Loop 与工具
    details: async generator 主循环、tool_call 与 tool_result 的配对不变量、副作用分级、只读并发与原序回填、大结果外置句柄。
    link: /core/agent-loop
  - icon: ◐
    title: 上下文工程
    details: 水位估算、旧结果清理、摘要压缩的切点规则与增量更新、计划复述、前缀缓存与“只追加”约束。
    link: /core/context-engineering
  - icon: ⛁
    title: 可恢复的长任务
    details: Session / Run / Step / Event 四层模型、CAS 状态机、intent→result journal、kill -9 之后副作用恰好一次。
    link: /reliability/checkpoint-recovery
  - icon: ⏹
    title: 取消、重试与幂等
    details: 协作式取消与“结果未知”、错误分类与全抖动退避、熔断与降级、幂等键与对账。
    link: /reliability/cancellation
  - icon: ⚖
    title: 策略与安全
    details: Hooks、四种权限模式、命令风险分析、污点感知、持久化人审、沙箱路线与供应链安全。
    link: /governance/hooks
  - icon: ⧉
    title: 多 Agent 与度量
    details: 子代理回传契约、DAG 调度、轨迹与确定性回放、带置信区间的评测、OTel GenAI span 与成本。
    link: /multi-agent/subagents
---

<div style="max-width: 960px; margin: 48px auto 0; padding: 0 24px;">

## 这套教程怎么读

| 你想… | 从这里开始 |
|---|---|
| 先建立整体概念 | [01 什么是 Harness](/guide/what-is-harness) → [03 架构总览](/guide/architecture) |
| 边跑边学 | [02 快速上手](/guide/quickstart)，然后打开 Web UI 右侧的“运行 / 上下文 / 事件 / 会话树” |
| 深入某个机制 | 按 [知识地图](/reference/knowledge-map) 找到对应章节、源码与可运行示例 |
| 自己动手实现 | [动手练习：从零实现 8 个组件](/reference/exercises)，每题都有验收标准和参考实现 |

每一章都遵循同一条主线：**要解决什么问题 → 业界怎么做 → flywhl-harness 怎么实现（源码精读）→ 取舍与常见坑 → 动手验证**。
文中的源码片段全部摘自 `src/flywhl_harness/`，行为都有对应的测试或示例可以复现。

</div>
