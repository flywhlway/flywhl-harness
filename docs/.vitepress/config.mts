import { readFileSync } from "node:fs";
import { defineConfig } from "vitepress";
import { withMermaid } from "vitepress-plugin-mermaid";

// 部署到子路径时设置 BASE，例如 GitHub Pages：BASE=/flywhl-harness/ npm run build
const base = process.env.BASE || "/";

// 页面标注的版本和包版本同源，发版时不用再改站点
const version = readFileSync(new URL("../../src/flywhl_harness/__init__.py", import.meta.url), "utf8")
  .match(/__version__ = "(.+?)"/)![1];
const changelog = "https://github.com/flywhlway/flywhl-harness/blob/main/CHANGELOG.md";

const sidebar = [
  {
    text: "入门",
    items: [
      { text: "01 什么是 Harness", link: "/guide/what-is-harness" },
      { text: "02 快速上手", link: "/guide/quickstart" },
      { text: "03 架构总览：一次请求的旅程", link: "/guide/architecture" },
    ],
  },
  {
    text: "核心机制",
    items: [
      { text: "04 Agent Loop", link: "/core/agent-loop" },
      { text: "05 消息模型与模型接入层", link: "/core/messages-and-models" },
      { text: "06 工具系统（ACI）", link: "/core/tools" },
      { text: "07 上下文工程与压缩", link: "/core/context-engineering" },
      { text: "08 终止条件、预算与循环守卫", link: "/core/termination-budget" },
    ],
  },
  {
    text: "可靠性",
    items: [
      { text: "09 状态与持久化：四层模型", link: "/reliability/state-persistence" },
      { text: "10 Run 生命周期状态机", link: "/reliability/run-lifecycle" },
      { text: "11 检查点与崩溃恢复", link: "/reliability/checkpoint-recovery" },
      { text: "12 取消语义", link: "/reliability/cancellation" },
      { text: "13 重试、熔断与降级", link: "/reliability/retry-fallback" },
      { text: "14 幂等与副作用", link: "/reliability/idempotency" },
      { text: "15 流式输出、插话与断线续传", link: "/reliability/streaming" },
    ],
  },
  {
    text: "策略与执行环境",
    items: [
      { text: "16 Hooks：把规则变成代码", link: "/governance/hooks" },
      { text: "17 权限与人审", link: "/governance/permissions-hitl" },
      { text: "18 Workspace 与沙箱", link: "/governance/workspace-sandbox" },
      { text: "19 安全：注入、凭据与供应链", link: "/governance/security" },
    ],
  },
  {
    text: "多 Agent 与度量",
    items: [
      { text: "20 子代理与回传契约", link: "/multi-agent/subagents" },
      { text: "21 DAG 调度与 Swarm", link: "/multi-agent/swarm-dag" },
      { text: "22 轨迹、回放与评测", link: "/multi-agent/trajectory-eval" },
      { text: "23 可观测性与成本", link: "/multi-agent/observability-cost" },
    ],
  },
  {
    text: "进阶专题",
    items: [
      { text: "24 长时任务 Harness", link: "/advanced/long-running" },
      { text: "25 持久执行引擎映射", link: "/advanced/durable-execution" },
      { text: "26 端云协同", link: "/advanced/edge-cloud" },
      { text: "27 协议生态：MCP、A2A 与 Skills", link: "/advanced/protocols" },
      { text: "28 可删减的 Harness", link: "/advanced/evolving-harness" },
      { text: "29 对照 pi：极简 Harness 的取舍", link: "/advanced/pi-comparison" },
      { text: "30 扩展指南", link: "/advanced/extending" },
    ],
  },
  {
    text: "参考",
    items: [
      { text: "知识地图", link: "/reference/knowledge-map" },
      { text: "动手练习：从零实现 8 个组件", link: "/reference/exercises" },
      { text: "源码地图", link: "/reference/source-map" },
      { text: "术语表", link: "/reference/glossary" },
      { text: "参考资料", link: "/reference/references" },
    ],
  },
];

// 精简手册自成一套目录：进入 /manual/ 后侧栏只显示手册页面，其它路径仍用上面的教程目录
const manualSidebar = [
  {
    text: "精简手册",
    items: [
      { text: "导读", link: "/manual/" },
      { text: "Harness 理论精要", link: "/manual/theory" },
      { text: "架构与组件", link: "/manual/architecture" },
      { text: "关键机制", link: "/manual/mechanisms" },
      { text: "安全与治理", link: "/manual/security" },
      { text: "实战手册", link: "/manual/practice" },
      { text: "速查表", link: "/manual/cheatsheet" },
    ],
  },
  {
    text: "完整教程",
    items: [{ text: "从 01 章开始读", link: "/guide/what-is-harness" }],
  },
];

// 英文站路径和中文站一一对应（/en/core/tools ↔ /core/tools），语言切换才能停在同一章。
// 除快速上手和架构总览外都是摘要，正文链到中文全文；精简手册和其余参考页没有英文版
const enSidebar = [
  {
    text: "Getting started",
    items: [
      { text: "01 What is a harness", link: "/en/guide/what-is-harness" },
      { text: "02 Quick start", link: "/en/guide/quickstart" },
      { text: "03 Architecture overview", link: "/en/guide/architecture" },
    ],
  },
  {
    text: "Core mechanisms",
    items: [
      { text: "04 The agent loop", link: "/en/core/agent-loop" },
      { text: "05 Messages and model access", link: "/en/core/messages-and-models" },
      { text: "06 The tool system (ACI)", link: "/en/core/tools" },
      { text: "07 Context engineering and compaction", link: "/en/core/context-engineering" },
      { text: "08 Termination, budgets and loop guards", link: "/en/core/termination-budget" },
    ],
  },
  {
    text: "Reliability",
    items: [
      { text: "09 State and persistence", link: "/en/reliability/state-persistence" },
      { text: "10 The run state machine", link: "/en/reliability/run-lifecycle" },
      { text: "11 Checkpoints and crash recovery", link: "/en/reliability/checkpoint-recovery" },
      { text: "12 Cancellation semantics", link: "/en/reliability/cancellation" },
      { text: "13 Retries, circuit breaking and fallback", link: "/en/reliability/retry-fallback" },
      { text: "14 Idempotency and side effects", link: "/en/reliability/idempotency" },
      { text: "15 Streaming, steering and resumption", link: "/en/reliability/streaming" },
    ],
  },
  {
    text: "Policy and execution",
    items: [
      { text: "16 Hooks", link: "/en/governance/hooks" },
      { text: "17 Permissions and human approval", link: "/en/governance/permissions-hitl" },
      { text: "18 Workspace and sandbox", link: "/en/governance/workspace-sandbox" },
      { text: "19 Security: injection, credentials, supply chain", link: "/en/governance/security" },
    ],
  },
  {
    text: "Multi-agent and measurement",
    items: [
      { text: "20 Subagents and return contracts", link: "/en/multi-agent/subagents" },
      { text: "21 DAG scheduling and swarms", link: "/en/multi-agent/swarm-dag" },
      { text: "22 Trajectories, replay and evaluation", link: "/en/multi-agent/trajectory-eval" },
      { text: "23 Observability and cost", link: "/en/multi-agent/observability-cost" },
    ],
  },
  {
    text: "Advanced topics",
    items: [
      { text: "24 Long-running harnesses", link: "/en/advanced/long-running" },
      { text: "25 Mapping to durable execution engines", link: "/en/advanced/durable-execution" },
      { text: "26 Edge-cloud collaboration", link: "/en/advanced/edge-cloud" },
      { text: "27 Protocols: MCP, A2A and Skills", link: "/en/advanced/protocols" },
      { text: "28 The removable harness", link: "/en/advanced/evolving-harness" },
      { text: "29 Compared with pi", link: "/en/advanced/pi-comparison" },
      { text: "30 Extension guide", link: "/en/advanced/extending" },
    ],
  },
  {
    text: "Reference",
    items: [{ text: "Glossary", link: "/en/reference/glossary" }],
  },
];

export default withMermaid(
  defineConfig({
    base,
    lang: "zh-CN",
    title: "flywhl harness",
    description: "从零手写 Agent Harness：原理、设计取舍与源码精读",
    cleanUrls: true,
    // quality-audit/ 是仓库内部的审计记录：链接指向目录、测试脚本与证据文件，按 GitHub 浏览写的，不进站点
    srcExclude: ["quality-audit/**"],
    lastUpdated: true,
    sitemap: { hostname: "https://flywhlway.github.io/flywhl-harness/" },
    head: [["link", { rel: "icon", href: `${base}favicon.svg` }]],
    locales: {
      root: { label: "简体中文", lang: "zh-CN" },
      en: {
        label: "English",
        lang: "en",
        link: "/en/",
        title: "flywhl harness",
        description: "An Agent Harness written by hand: principles, design trade-offs and source walkthroughs",
        themeConfig: {
          nav: [
            { text: "Docs", link: "/en/guide/what-is-harness" },
            { text: "Quick start", link: "/en/guide/quickstart" },
            { text: "Glossary", link: "/en/reference/glossary" },
            { text: `Docs for ${version}`, link: changelog },
          ],
          sidebar: { "/en/": enSidebar },
          outline: { level: [2, 3], label: "On this page" },
          docFooter: { prev: "Previous", next: "Next" },
          lastUpdated: { text: "Last updated" },
          darkModeSwitchLabel: "Theme",
          sidebarMenuLabel: "Menu",
          returnToTopLabel: "Back to top",
          langMenuLabel: "Language",
          footer: { message: "MIT License", copyright: "flywhl-harness docs" },
          notFound: {
            title: "PAGE NOT FOUND",
            quote: "This page may have no English version yet. The Chinese tutorial is the complete text.",
            linkText: "Back to the English home page",
          },
        },
      },
    },
    markdown: { lineNumbers: true, theme: { light: "github-light", dark: "github-dark" } },
    themeConfig: {
      logo: "/favicon.svg",
      siteTitle: "flywhl / harness",
      nav: [
        { text: "教程", link: "/guide/what-is-harness" },
        { text: "精简手册", link: "/manual/", activeMatch: "^/manual/" },
        { text: "知识地图", link: "/reference/knowledge-map" },
        { text: "源码地图", link: "/reference/source-map" },
        { text: `对应 ${version}`, link: changelog },
      ],
      sidebar: { "/manual/": manualSidebar, "/": sidebar },
      outline: { level: [2, 3], label: "本页目录" },
      search: {
        provider: "local",
        options: {
          locales: {
            root: {
              translations: {
                button: { buttonText: "搜索", buttonAriaLabel: "搜索" },
                modal: { noResultsText: "没有找到结果", resetButtonTitle: "清除", footer: { selectText: "选择", navigateText: "切换", closeText: "关闭" } },
              },
            },
          },
        },
      },
      docFooter: { prev: "上一章", next: "下一章" },
      lastUpdated: { text: "最后更新" },
      darkModeSwitchLabel: "主题",
      sidebarMenuLabel: "目录",
      returnToTopLabel: "回到顶部",
      footer: { message: "MIT License", copyright: "flywhl-harness 系列教程" },
    },
    mermaid: { theme: "neutral" },
    vite: {
      optimizeDeps: {
        // dev 模式下必须整体预构建 mermaid：它的传递依赖里有 CJS 包（fastdom、cytoscape-fcose、
        // dayjs/plugin/duration 等），vitepress-plugin-mermaid 只硬编码了旧版 mermaid 的几个，
        // 漏掉的会让浏览器报 "does not provide an export named 'default'"，整站白屏。
        include: ["mermaid"],
      },
    },
  }),
);
