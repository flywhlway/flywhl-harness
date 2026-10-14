import { defineConfig } from "vitepress";
import { withMermaid } from "vitepress-plugin-mermaid";

// 部署到子路径时设置 BASE，例如 GitHub Pages：BASE=/flywhl-harness/ npm run build
const base = process.env.BASE || "/";

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

export default withMermaid(
  defineConfig({
    base,
    lang: "zh-CN",
    title: "flywhl harness",
    description: "从零手写 Agent Harness：原理、设计取舍与源码精读",
    cleanUrls: true,
    lastUpdated: true,
    head: [["link", { rel: "icon", href: `${base}favicon.svg` }]],
    markdown: { lineNumbers: true, theme: { light: "github-light", dark: "github-dark" } },
    themeConfig: {
      logo: "/favicon.svg",
      siteTitle: "flywhl / harness",
      nav: [
        { text: "教程", link: "/guide/what-is-harness" },
        { text: "知识地图", link: "/reference/knowledge-map" },
        { text: "源码地图", link: "/reference/source-map" },
      ],
      sidebar,
      outline: { level: [2, 3], label: "本页目录" },
      search: {
        provider: "local",
        options: {
          translations: {
            button: { buttonText: "搜索", buttonAriaLabel: "搜索" },
            modal: { noResultsText: "没有找到结果", resetButtonTitle: "清除", footer: { selectText: "选择", navigateText: "切换", closeText: "关闭" } },
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
