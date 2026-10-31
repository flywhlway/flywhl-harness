# 精简手册

**Agent = Model + Harness。** 模型只决定“下一句说什么、调哪个工具”；让它连续工作几小时、崩溃能续上、越权会被拦下，靠的是模型之外的一切——循环、工具、上下文、状态、生命周期、策略、执行环境与度量，统称 Harness。

本手册把 [30 章教程](/guide/what-is-harness) 压缩成 7 页，只保留结论、机制与用法；每页末尾列出可以深入的章节。

## 全景

```mermaid
flowchart TB
  IN["入口：CLI · Web UI · Python 库"]
  H["Harness：Run 管理 · 状态机 · 心跳租约"]
  DB[("SQLite：会话 · Run · Step · 事件")]
  LOOP["AgentLoop：回合循环"]
  CTX["上下文：清理 · 压缩 · 外置"]
  LLM["模型接入：重试 · 熔断 · 降级"]
  EXE["工具执行器"]
  POL["策略：权限 · Hooks · 审批"]
  TOOLS["工具：文件 · bash · MCP · 子代理"]
  IN -- 输入 --> H
  H -- 驱动 --> LOOP
  H -- 先落盘 --> DB
  DB -. 再推事件 .-> IN
  LOOP --> CTX & LLM & EXE
  EXE --> POL & TOOLS
```

入口把输入交给 `Harness`，它创建 Run 并在后台驱动 `AgentLoop`。Loop 每一轮先整形上下文、再调用模型，把模型要求的工具调用交给执行器；执行器让每次调用逐道通过策略关卡后才真正执行。所有变化先写进 SQLite，再作为事件推给三种入口。

## 阅读路径

| 目标 | 顺序 |
|---|---|
| 十分钟跑起来 | [实战手册](./practice) → [速查表](./cheatsheet) |
| 理解为什么这样设计 | [理论精要](./theory) → [架构与组件](./architecture) → [关键机制](./mechanisms) |
| 上线前评估风险 | [安全与治理](./security) |

## 项目速览

- Python 3.11+，核心只依赖 `httpx`；Web 层是 FastAPI + SSE；状态存 SQLite（WAL 模式）。
- 三种入口：终端 `flywhl chat` / `flywhl run`、Web UI `flywhl serve`、Python 库 `Harness`。
- 模型写成 `<供应商>:<模型>`，接入 DeepSeek、Anthropic、OpenAI 兼容服务、百炼、Kimi、智谱、MiniMax、OpenRouter 与本地 Ollama；一个密钥都没有时，内置演示模型 `flywhl-demo` 按剧本演示全部机制。
