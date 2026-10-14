# 参考资料

教程中涉及的业界做法主要来自以下公开资料（2026 年 10 月查阅）。行业事实变化很快，具体细节以原文与官方文档为准。

## Harness 设计

- Anthropic Engineering — *Effective harnesses for long-running agents*（2025-11）
  https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents
- Anthropic Engineering — *Scaling Managed Agents: Decoupling the brain from the hands*（2026-04）
  https://www.anthropic.com/engineering/managed-agents
- Anthropic Engineering — *Harness design for long-running application development*（2026-03）
  https://www.anthropic.com/engineering/harness-design-long-running-apps
- Anthropic Engineering — *Effective context engineering for AI agents*（2025-09）
  https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Anthropic Engineering — *Writing effective tools for agents — with agents*（2025-09）
  https://www.anthropic.com/engineering/writing-tools-for-agents
- OpenAI — *Unrolling the Codex agent loop*（2026-01）
  https://openai.com/index/unrolling-the-codex-agent-loop/
- OpenAI — *Harness engineering: leveraging Codex in an agent-first world*
  https://openai.com/index/harness-engineering/
- Manus — *Context Engineering for AI Agents: Lessons from Building Manus*（2025-07）
  https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus
- LangChain — *Improving Deep Agents with harness engineering*（2026-02）
  https://www.langchain.com/blog/improving-deep-agents-with-harness-engineering

## 开源实现

- pi（原 badlogic/pi-mono）— 极简 coding agent harness 的 TypeScript monorepo
  https://github.com/earendil-works/pi
- Mario Zechner — *What I learned building an opinionated and minimal coding agent*（2025-11）
  https://mariozechner.at/posts/2025-11-30-pi-coding-agent/
- LangChain deepagents
  https://docs.langchain.com/oss/python/deepagents/overview

## 工具、权限与 Hooks

- Claude Code 文档：Hooks、Permissions、Sub-agents、Context window
  https://code.claude.com/docs/en/hooks · https://code.claude.com/docs/en/permissions · https://code.claude.com/docs/en/sub-agents
- Model Context Protocol 规范
  https://modelcontextprotocol.io
- Agent2Agent（A2A）协议
  https://a2a-protocol.org

## 持久执行与可观测性

- Temporal — OpenAI Agents SDK 集成
  https://docs.temporal.io/develop/python/integrations/openai-agents
- Temporal — Agent Harness 介绍
  https://temporal.io/blog/temporal-agent-harness-durable-agent-infrastructure
- OpenTelemetry GenAI 语义约定（agent spans）
  https://github.com/open-telemetry/semantic-conventions-genai

## 模型 API

- Anthropic Messages API：流式事件、工具使用、提示缓存、thinking、refusal 与 fallbacks
  https://platform.claude.com/docs
- OpenAI Chat Completions 流式接口（OpenAI 兼容服务普遍遵循的协议）
  https://platform.openai.com/docs/api-reference/chat

## 安全

- OWASP Top 10 for Agentic Applications
  https://genai.owasp.org
