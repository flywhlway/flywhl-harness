// 领域元数据：状态、副作用、权限模式的展示文案与语义色调。所有视图共用这一处定义，
// 颜色只是辅助——每个状态都同时有文字与图标，不单靠颜色传达含义。
import { esc } from "./dom.js";
import { icon } from "./icons.js";

export const TERMINAL = new Set(["completed", "failed", "cancelled", "timed_out", "budget_exhausted", "loop_detected", "incomplete"]);
export const ACTIVE = new Set(["queued", "running", "waiting_human", "cancelling"]);

export const RUN_STATUS = {
  created: { label: "已创建", tone: "", icon: "clock" },
  queued: { label: "排队中", tone: "", icon: "clock" },
  running: { label: "运行中", tone: "brand", icon: "activity" },
  waiting_human: { label: "等待审批", tone: "warning", icon: "shieldAlert" },
  paused: { label: "已暂停", tone: "", icon: "pauseCircle" },
  interrupted: { label: "已中断", tone: "warning", icon: "alert" },
  cancelling: { label: "正在停止", tone: "warning", icon: "stop" },
  completed: { label: "已完成", tone: "success", icon: "checkCircle" },
  incomplete: { label: "未完成", tone: "warning", icon: "alert" },
  failed: { label: "失败", tone: "danger", icon: "xCircle" },
  budget_exhausted: { label: "预算耗尽", tone: "warning", icon: "gauge" },
  loop_detected: { label: "检测到循环", tone: "danger", icon: "repeat" },
  timed_out: { label: "超时", tone: "danger", icon: "clock" },
  cancelled: { label: "已取消", tone: "", icon: "ban" },
};

export function statusBadge(status, { square = false, still = false } = {}) {
  const m = RUN_STATUS[status] || { label: status || "未开始", tone: "", icon: "info" };
  // 只有“此刻正在运行”才用转动的图标；历史记录里的 running 用静态图标，避免误以为仍在执行
  const ic = status === "running" && !still ? '<span class="spinner" style="width:10px;height:10px;border-width:1.5px" aria-hidden="true"></span>' : icon(m.icon);
  return `<span class="badge ${m.tone}${square ? " square" : ""}">${ic}${esc(m.label)}</span>`;
}

export function statusTone(status) { return (RUN_STATUS[status] || {}).tone || ""; }

export const TOOL_STATUS = {
  ok: { label: "完成", cls: "ok", icon: "check" },
  reconciled: { label: "已对账", cls: "ok", icon: "check" },
  error: { label: "出错", cls: "err", icon: "x" },
  invalid: { label: "参数无效", cls: "err", icon: "x" },
  denied: { label: "权限拒绝", cls: "err", icon: "ban" },
  rejected: { label: "已拒绝", cls: "warn", icon: "ban" },
  cancelled: { label: "已取消", cls: "", icon: "ban" },
  skipped: { label: "未执行", cls: "", icon: "ban" },
  unknown: { label: "结果未知", cls: "warn", icon: "alert" },
  timeout: { label: "超时", cls: "err", icon: "clock" },
  truncated: { label: "参数被截断", cls: "warn", icon: "alert" },
};

export const EFFECT = {
  read: { label: "只读", tone: "", hint: "不改变任何状态，可自动执行" },
  write: { label: "写入", tone: "warning", hint: "修改工作区文件，执行前会做快照" },
  external: { label: "外部", tone: "danger", hint: "影响工作区之外的系统，结果可能不可撤回" },
};
export const effectBadge = (fx) => `<span class="badge square fx ${(EFFECT[fx] || EFFECT.read).tone}" title="${esc((EFFECT[fx] || EFFECT.read).hint)}">${esc((EFFECT[fx] || EFFECT.read).label)}</span>`;

export const RISK = {
  low: { label: "低风险", tone: "" },
  medium: { label: "中风险", tone: "warning" },
  high: { label: "高风险", tone: "danger" },
  critical: { label: "严重", tone: "danger" },
};

export const MODES = [
  { id: "default", label: "默认", icon: "shield", desc: "只读操作自动放行；写入文件、执行命令与外部操作需要确认。" },
  { id: "accept_edits", label: "自动接受编辑", icon: "filePen", desc: "工作区内的文件编辑自动放行；命令与外部操作仍需确认。" },
  { id: "plan", label: "规划", icon: "eye", desc: "只允许只读工具，适合先给出方案、再决定是否执行。" },
  { id: "bypass", label: "完全放行", icon: "zap", danger: true, desc: "所有操作直接执行、不再确认。仅应在隔离沙箱中使用。" },
];
export const modeOf = (id) => MODES.find((m) => m.id === id) || MODES[0];

const TOOL_ICONS = {
  read: "file", write: "filePlus", edit: "filePen", ls: "folder", grep: "search", bash: "terminal",
  todo_write: "listChecks", read_ref: "fileSearch", search_ref: "fileSearch", task: "bot",
  send_notification: "send", sleep: "timer",
};
export const toolIcon = (name) => (name?.startsWith("mcp__") ? "plug" : TOOL_ICONS[name] || "wrench");

/** 工具调用的一行摘要：让人不展开也能看懂“它要做什么” */
export function callSummary(name, a = {}) {
  if (name === "bash") return a.command || "";
  if (name === "grep") return `${a.pattern || ""}${a.glob ? "  ·  " + a.glob : ""}`;
  if (name === "task") return a.description || "";
  if (name === "send_notification") return `${a.to || ""}：${a.message || ""}`;
  if (name === "todo_write") return `更新计划（${(a.todos || []).length} 项）`;
  if (name === "sleep") return `等待 ${a.seconds}s`;
  if (name === "edit" && a.path) return a.path;
  if (a.path) return a.path + (a.offset ? `  ·  从第 ${a.offset} 行` : "");
  if (a.ref) return a.ref;
  return Object.values(a).map((v) => (typeof v === "string" ? v : JSON.stringify(v))).join("  ").slice(0, 120);
}

/** 开始页模板：演示模型下给出能完整走一遍机制的示例；真实模型下给出常见任务 */
export const DEMO_TEMPLATES = [
  { prompt: "列出目录", icon: "folder", title: "浏览工作区", desc: "只读工具，自动执行" },
  { prompt: "创建 hello.py", icon: "filePlus", title: "创建文件", desc: "写入操作：审批卡片与 diff 预览" },
  { prompt: "运行 `python3 hello.py`", icon: "terminal", title: "执行命令", desc: "命令风险分析与流式输出" },
  { prompt: "并行检查", icon: "layers", title: "并行只读调用", desc: "同一轮多个调用并发执行、按序回填" },
  { prompt: "制定计划", icon: "listChecks", title: "制定计划", desc: "todo 列表与计划复述" },
  { prompt: "委托子任务", icon: "bot", title: "委托子代理", desc: "隔离上下文与结构化回传契约" },
  { prompt: "等待 20 秒", icon: "timer", title: "长时间操作", desc: "试试插话、暂停与停止" },
  { prompt: "发送通知", icon: "send", title: "外部副作用", desc: "幂等键与不可中断操作" },
  { prompt: "压缩演示", icon: "database", title: "上下文压缩", desc: "水位、清理与结构化摘要" },
  { prompt: "循环测试", icon: "repeat", title: "循环检测", desc: "重复调用先提醒、再终止" },
  { prompt: "错误演示", icon: "alert", title: "错误处理", desc: "失败如何变成模型可读的结果" },
  { prompt: "帮助", icon: "info", title: "帮助", desc: "查看演示模型支持的指令" },
];
export const REAL_TEMPLATES = [
  { prompt: "总结这个项目的结构", icon: "folder", title: "了解项目", desc: "阅读目录与关键文件，给出结构说明" },
  { prompt: "找出所有 TODO 并列成计划", icon: "listChecks", title: "整理待办", desc: "搜索代码中的 TODO 并制定计划" },
  { prompt: "运行测试，失败的话修复它", icon: "terminal", title: "修复测试", desc: "执行测试、定位并修复失败用例" },
];

export const SYS_KINDS = {
  reminder: { label: "系统提醒", icon: "info" },
  resume_note: { label: "恢复说明", icon: "history", attention: true },
  stop_feedback: { label: "验收未通过，继续执行", icon: "alert", attention: true },
  wrap_up: { label: "预算收尾指令", icon: "gauge" },
  compaction: { label: "压缩摘要", icon: "database" },
};
export const USER_KINDS = { steer: { label: "插话", icon: "steer" }, follow_up: { label: "排队", icon: "queue" } };

export const EVENT_GROUPS = {
  run: ["run_start", "run_end", "status", "turn_start", "turn_end", "usage"],
  msg: ["message_start", "message_update", "message_end", "model_retry"],
  tool: ["tool_start", "tool_update", "tool_end"],
  policy: ["approval_request", "approval_resolved"],
  ctx: ["compaction_start", "compaction_end"],
};
export const EVENT_TYPES = Object.values(EVENT_GROUPS).flat().concat(["notice", "error"]);
export function eventGroup(type) {
  for (const [g, list] of Object.entries(EVENT_GROUPS)) if (list.includes(type)) return g;
  return "other";
}
