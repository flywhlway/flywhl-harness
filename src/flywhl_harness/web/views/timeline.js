// 对话时间线的渲染器。每个函数只负责“把一条持久化记录或一个事件画成 DOM”，不持有状态；
// 交互所需的上下文（工具副作用表、当前会话、回调）通过 ctx 传入。
import { api } from "../lib/api.js";
import { $, esc, fmt, h } from "../lib/dom.js";
import { icon } from "../lib/icons.js";
import { RISK, SYS_KINDS, TOOL_STATUS, USER_KINDS, callSummary, effectBadge, statusBadge, toolIcon } from "../lib/meta.js";
import { promptDialog, toast } from "../lib/ui.js";
import { md } from "../md.js";

const blocks = (m, type) => (m.content || []).filter((b) => b.type === type);
export const textOf = (m) => blocks(m, "text").map((b) => b.text).join("");
const spinner = (size = 12) => `<span class="spinner" style="width:${size}px;height:${size}px;border-width:1.5px" aria-hidden="true"></span>`;

export function callout(tone, text, iconName) {
  const ic = iconName || { info: "info", warning: "alert", danger: "xCircle" }[tone] || "info";
  return h("div", { class: `callout ${tone}`, html: `<span class="ic">${icon(ic)}</span><div class="callout-text">${esc(text)}</div>` });
}

// ====================================================================== 消息
export function userItem(m) {
  const kind = m.meta?.kind || "prompt";
  const text = (m.content || []).map((b) => b.text || "").join("");
  if (SYS_KINDS[kind]) return sysItem(kind, text);
  const tag = USER_KINDS[kind];
  return h("div", { class: "msg-user", html: `<div class="bubble">${tag ? `<span class="tag badge brand square">${icon(tag.icon)}${esc(tag.label)}</span>` : ""}${esc(text)}</div>` });
}

/** harness 注入给模型的消息（提醒、恢复说明、验收反馈……）：默认折叠，需要人注意的默认展开 */
export function sysItem(kind, text) {
  const k = SYS_KINDS[kind];
  const d = h("details", { class: `disclosure system${k.attention ? " attention" : ""}` });
  d.open = !!k.attention;
  d.innerHTML = `<summary>${icon("chevronRight", "sm chev")}${icon(k.icon)}<span>${esc(k.label)}</span><span class="aside">由 harness 注入，模型可见</span></summary>
    <div class="content"><pre class="plain">${esc(text.replace(/<\/?\s*system-reminder[^>]*>/gi, "").trim())}</pre></div>`;
  return d;
}

export function thinkingBlock(text, { open = false, live = false } = {}) {
  const d = h("details", { class: "disclosure thinking" });
  d.open = open;
  d.innerHTML = `<summary>${icon("chevronRight", "sm chev")}<span class="lbl">${live ? "正在思考…" : "思考过程"}</span><span class="aside tnum">${fmt.int(text.length)} 字</span></summary>
    <div class="content"><pre class="plain"></pre></div>`;
  $("pre", d).textContent = text;
  return d;
}

export function assistantItem(m, results, ctx) {
  const w = h("article", { class: "msg-assistant" + (m.stop_reason === "aborted" ? " aborted" : ""), id: "m-" + m.id, "aria-label": "助手回复" });
  const thinking = blocks(m, "thinking").map((b) => b.thinking).join("");
  const text = textOf(m);
  if (thinking) w.append(thinkingBlock(thinking));
  if (text) w.append(h("div", { class: "md", html: md(text) }));
  if (m.stop_reason === "error") w.append(callout("danger", "模型调用失败：" + (m.error || "未知错误")));
  if (m.stop_reason === "refusal") w.append(callout("warning", "模型基于安全策略拒绝了这个请求"));
  if (m.stop_reason === "aborted") w.append(callout("info", "输出在生成过程中被取消，以上是已生成的部分"));
  const calls = blocks(m, "tool_call");
  if (calls.length) {
    const tools = h("div", { class: "tools" });
    for (const c of calls) tools.append(toolCard(c, results[c.id], ctx));
    w.append(tools);
  }
  const u = m.usage;
  if (u || m.model) {
    const bits = [];
    if (m.model) bits.push(`<span>${icon("cpu", "sm")}${esc(m.model)}</span>`);
    if (u) bits.push(`<span class="tnum" title="输入（含缓存命中）/ 输出 token">${icon("arrowUp", "sm")}${fmt.k(fmt.tokIn(u))} ${icon("arrowDown", "sm")}${fmt.k(u.output_tokens)}</span>`);
    if (u?.cache_read_tokens) bits.push(`<span class="tnum">缓存命中 ${fmt.k(u.cache_read_tokens)}</span>`);
    if (m.stop_reason === "max_tokens") bits.push(`<span>${icon("alert", "sm")}达到输出长度上限</span>`);
    w.append(h("div", { class: "msg-meta", html: bits.join("") }));
  }
  return w;
}

// ====================================================================== 工具调用
export function toolCard(call, result, ctx, { streaming = false } = {}) {
  // 优先用这次调用实际判定的副作用等级（bash 按命令判定），没有时才退回工具的静态等级
  const fx = result?.details?.effect || ctx.toolFx[call.name] || "read";
  const card = h("div", { class: "tool", id: "tc-" + call.id, "data-open": "false" });
  const bodyId = "tb-" + call.id;
  const head = h("button", { class: "tool-head", type: "button", "aria-expanded": "false", "aria-controls": bodyId });
  head.innerHTML = `<span class="tool-ic">${icon(toolIcon(call.name))}</span><span class="name">${esc(call.name)}</span>
    <span class="sum">${esc(callSummary(call.name, call.arguments))}</span>${effectBadge(fx)}
    <span class="state">${streaming ? `${spinner()}生成参数` : "等待执行"}</span>${icon("chevronRight", "sm chev")}`;
  head.onclick = () => setOpen(card, card.dataset.open !== "true");
  const body = h("div", { class: "tool-body", id: bodyId });
  body.innerHTML = `<div class="sec-args"><div class="block-label">参数</div><pre class="code-block args"></pre></div>
    <div class="sec-sub" hidden><div class="block-label">${icon("bot", "sm")}子代理进度</div><div class="sub-log"></div></div>
    <div class="sec-live" hidden><div class="block-label">实时输出</div><pre class="code-block live"></pre></div>
    <div class="sec-result"></div>`;
  if (!streaming) $(".args", body).textContent = JSON.stringify(call.arguments || {}, null, 2);
  card.append(head, body);
  if (result) setToolResult(card, result, ctx);
  return card;
}

export function setOpen(card, open) {
  card.dataset.open = String(open);
  $(".tool-head", card).setAttribute("aria-expanded", String(open));
}

export function setToolState(card, html, cls = "") {
  const st = $(".state", card);
  st.className = "state " + cls;
  st.innerHTML = html;
}

export function markRunning(card, d) {
  card.classList.add("running");
  const fx = card.querySelector(".badge.fx");
  if (fx && d.effect) fx.outerHTML = effectBadge(d.effect);
  if (d.summary) $(".sum", card).textContent = d.summary;
  setToolState(card, `${spinner()}${d.recovering ? "对账中" : "执行中"}`, "run");
}

export function appendLiveOutput(card, text) {
  const sec = $(".sec-live", card);
  sec.hidden = false;
  const pre = $("pre", sec);
  pre.textContent += text;
  if (pre.textContent.length > 60000) pre.textContent = pre.textContent.slice(-50000);
  pre.scrollTop = pre.scrollHeight;
}

export function setToolResult(card, msg, ctx) {
  card.classList.remove("running");
  const d = msg.details || {};
  const fxBadge = card.querySelector(".badge.fx");
  if (fxBadge && d.effect) fxBadge.outerHTML = effectBadge(d.effect);
  const text = (msg.content || []).map((b) => b.text || "").join("");
  const st = d.status || (msg.is_error ? "error" : "ok");
  const meta = TOOL_STATUS[st] || { label: st, cls: msg.is_error ? "err" : "ok", icon: msg.is_error ? "x" : "check" };
  const dur = d.duration_ms != null ? `<span class="tnum muted">${fmt.dur(d.duration_ms / 1000)}</span>` : "";
  setToolState(card, `${icon(meta.icon, "sm")}${esc(meta.label)}${dur ? " · " + dur : ""}`, meta.cls);
  card.classList.toggle("error", !!msg.is_error && !["cancelled", "skipped", "rejected"].includes(st));

  const parts = [];
  const resultLabel = d.externalized_ref ? `结果 · 已外置为句柄 <code>${esc(d.externalized_ref)}</code>` : "结果";
  parts.push(`<div><div class="block-label">${resultLabel}</div><pre class="code-block">${esc(text)}</pre></div>`);
  if (d.diff) parts.push(`<div><div class="block-label">变更</div>${diffHtml(d.diff)}</div>`);
  if (d.todos) parts.push(`<div><div class="block-label">计划</div>${todoList(d.todos)}</div>`);
  if (d.contract) parts.push(`<div><div class="block-label">子代理回传契约</div><pre class="code-block">${esc(JSON.stringify(d.contract, null, 2))}</pre></div>`);
  const kv = [];
  if (d.snapshot) kv.push(`<span><b>执行前快照</b><code>${esc(d.snapshot.slice(0, 10))}</code></span>`);
  if (d.idempotency_key) kv.push(`<span><b>幂等键</b><code>${esc(d.idempotency_key)}</code>${d.fresh === false ? "（外部系统识别为重复请求，未重复执行）" : ""}</span>`);
  if (d.approval_wait_ms) kv.push(`<span><b>等待审批</b>${fmt.dur(d.approval_wait_ms / 1000)}</span>`);
  if (kv.length) parts.push(`<div class="kv-inline">${kv.join("")}</div>`);
  const box = $(".sec-result", card);
  box.innerHTML = parts.join("");
  box.style.display = "grid";
  box.style.gap = "12px";
  if (d.child_session_id) {
    const b = h("button", { class: "btn sm", type: "button", html: `${icon("external", "sm")}打开子代理的隔离会话` });
    b.onclick = (e) => { e.stopPropagation(); ctx.openSession(d.child_session_id); };
    box.append(h("div", {}, b));
  }
  if (card.classList.contains("error") || st === "unknown") setOpen(card, true);
}

export function diffHtml(text) {
  const cls = (l) => (l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : l.startsWith("@@") ? "hunk" : "");
  return `<pre class="code-block diff" aria-label="变更差异">${String(text).split("\n").map((l) => `<span class="${cls(l)}">${esc(l) || " "}</span>`).join("")}</pre>`;
}

const TODO_ICON = { completed: "checkCircle", in_progress: "activity", pending: "clock" };
export function todoList(todos) {
  return `<ul class="todo-list">${todos.map((t) => `<li class="${esc(t.status)}">${icon(TODO_ICON[t.status] || "clock", "sm")}<span>${esc(t.content)}</span></li>`).join("")}</ul>`;
}

export function subagentLine(card, sub, ctx) {
  const sec = $(".sec-sub", card);
  if (sec.hidden) { sec.hidden = false; setOpen(card, true); }
  const log = $(".sub-log", sec);
  const ev = sub.event || {}, d = ev.data || {};
  let ic = "", text = "";
  if (ev.type === "tool_start") { ic = toolIcon(d.name); text = `${d.name}  ${callSummary(d.name, d.args)}`; }
  else if (ev.type === "tool_end") { ic = d.is_error ? "x" : "check"; text = `${d.name} · ${(TOOL_STATUS[d.status] || {}).label || d.status}`; }
  else if (ev.type === "run_end") { ic = "commit"; text = `子代理结束：${d.status}`; }
  else if (ev.type === "approval_request") { ic = "shieldAlert"; text = `等待审批：${d.summary}`; ctx.onSubApproval?.(d, card); }
  else if (ev.type === "message_end" && d.message?.role === "assistant") {
    const t = textOf(d.message);
    if (t) { ic = "chat"; text = t.slice(0, 120) + (t.length > 120 ? "…" : ""); }
  }
  if (!text) return;
  log.append(h("div", { html: `${icon(ic, "sm")}<span>${esc(text)}</span>` }));
  log.scrollTop = log.scrollHeight;
}

// ====================================================================== 审批
/**
 * 审批卡片：展示“为什么要问你”（风险、原因、命令分析）、“会改成什么样”（diff 预览）、“实际要执行什么”（可编辑参数）。
 * ctx.onResolved(a, action) 在提交成功后调用。
 */
export function approvalCard(a, ctx) {
  const risk = RISK[a.risk] || RISK.medium;
  const id = "ap-" + a.id;
  const card = h("section", { class: "approval", id, "data-call": a.call_id, "aria-labelledby": id + "-t" });
  const fromOther = a.session_title && ctx.sid && a.session_id !== ctx.sid;
  card.innerHTML = `
    <div class="approval-head"><span class="ic">${icon("shieldAlert", "lg")}</span>
      <div style="min-width:0;flex:1"><div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <h3 id="${id}-t">需要你的确认</h3><span class="badge ${risk.tone}">${esc(risk.label)}</span>
        ${fromOther || ctx.showSource ? `<span class="badge outline">${icon("chat")}${esc(a.session_title || "")}</span>` : ""}</div>
        <p class="sub"><code>${esc(a.tool)}</code> ${esc(a.summary)}</p></div></div>
    <div class="approval-body">
      ${a.reason ? `<p style="font-size:var(--fs-sm);color:var(--text-2);line-height:1.6">${esc(a.reason)}</p>` : ""}
      ${a.analysis?.length ? `<div><div class="block-label">命令分析</div><ul class="reasons">${a.analysis.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></div>` : ""}
      ${a.preview ? `<div><div class="block-label">变更预览</div>${diffHtml(a.preview)}</div>` : ""}
      <details class="disclosure args-box"${a.preview ? "" : " open"}>
        <summary>${icon("chevronRight", "sm chev")}${icon("pencil")}<span>实际参数</span><span class="aside">可修改后批准，修改会重新校验并再次经过权限与 hook 检查</span></summary>
        <div class="content"><div class="field">
          <label class="sr-only" for="${id}-args">工具参数（JSON）</label>
          <textarea class="textarea mono" id="${id}-args" rows="6" spellcheck="false" aria-describedby="${id}-err" style="font-size:var(--fs-xs)"></textarea>
          <div class="field-error" id="${id}-err" role="alert" hidden></div></div></div>
      </details>
    </div>
    <div class="approval-foot">
      <span class="src">${icon("clock", "sm")} ${esc(fmt.ago(a.created_at))}${a.expires_at ? ` · ${esc(expiresIn(a.expires_at))}` : ""}</span>
      <span class="spacer"></span>
      <button class="btn danger-outline" type="button" data-act="reject">${icon("ban")}拒绝…</button>
      <button class="btn" type="button" data-act="always" data-tip="批准并为本会话添加授权规则：同类操作不再询问">${icon("shieldCheck")}本会话始终允许</button>
      <button class="btn primary" type="button" data-act="approve">${icon("check")}批准</button>
    </div>`;
  const ta = $("textarea", card);
  ta.value = JSON.stringify(a.args, null, 2);
  const err = $(".field-error", card);
  const showErr = (msg) => { err.hidden = !msg; err.innerHTML = msg ? `${icon("alert", "sm")}<span>${esc(msg)}</span>` : ""; ta.classList.toggle("invalid", !!msg); };
  ta.addEventListener("blur", () => { try { JSON.parse(ta.value); showErr(""); } catch { showErr("参数不是合法的 JSON，请检查括号与引号"); } });

  card.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn || card.classList.contains("resolving")) return;
    const act = btn.dataset.act;
    let edited = null;
    try {
      const v = JSON.parse(ta.value);
      if (JSON.stringify(v) !== JSON.stringify(a.args)) edited = v;
      showErr("");
    } catch {
      $(".args-box", card).open = true;
      showErr("参数不是合法的 JSON，请检查括号与引号");
      ta.focus();
      return;
    }
    let reason = "";
    if (act === "reject") {
      reason = await promptDialog({
        title: "拒绝这个操作", desc: "拒绝理由会回传给模型，帮助它调整方案（可以留空）。", label: "拒绝理由",
        placeholder: "例如：不要修改生产配置，改为输出建议", confirmText: "拒绝操作", danger: true,
      });
      if (reason === null) return;
    }
    card.classList.add("resolving");
    btn.insertAdjacentHTML("afterbegin", spinner(14));
    try {
      await api.post(`/api/approvals/${a.id}`, { approved: act !== "reject", reason, edited_args: edited, remember: act === "always" });
      toast(act === "reject" ? "已拒绝，理由已回传给模型" : act === "always" ? "已批准，并为本会话添加了授权规则" : edited ? "已按修改后的参数批准" : "已批准",
        { tone: act === "reject" ? "info" : "success" });
      ctx.onResolved?.(a, act);
    } catch (ex) {
      card.classList.remove("resolving");
      btn.querySelector(".spinner")?.remove();
      toast(ex.message, { tone: "danger", title: "提交审批失败" });
      if (ex.status === 409 || ex.status === 404) ctx.onResolved?.(a, "stale");   // 已被别处处理或已过期
    }
  });
  return card;
}

function expiresIn(ts) {
  const s = ts - Date.now() / 1000;
  if (s <= 0) return "已过期";
  return s < 120 ? `${Math.round(s)} 秒后超时拒绝` : `${Math.round(s / 60)} 分钟后超时拒绝`;
}

// ====================================================================== 分隔线 / 小结 / 提示
export function compactionDivider(p) {
  const wrap = h("div", { style: "display:grid;gap:8px" });
  const line = h("div", { class: "timeline-divider" });
  const inner = h("span", { class: "inner", html: `${icon("database", "sm")}上下文已压缩 <span class="tnum">${fmt.k(p.tokens_before)}${p.tokens_after ? " → " + fmt.k(p.tokens_after) : ""}</span> tokens · 摘要了 ${esc(p.summarized_messages ?? p.summarized ?? "?")} 条消息` });
  const btn = h("button", { class: "btn ghost sm", type: "button", "aria-expanded": "false" }, "查看摘要");
  inner.append(btn);
  line.append(inner);
  wrap.append(line);
  btn.onclick = () => {
    const open = btn.getAttribute("aria-expanded") === "true";
    btn.setAttribute("aria-expanded", String(!open));
    btn.textContent = open ? "查看摘要" : "收起摘要";
    if (open) { wrap.querySelector(".disclosure")?.remove(); return; }
    const d = h("div", { class: "disclosure", html: `<div class="content" style="border-top:0"><pre class="plain"></pre></div>` });
    $("pre", d).textContent = (p.summary || "") + (p.goal ? "\n\n## 原始任务（原文保留）\n" + p.goal : "");
    wrap.append(d);
  };
  return wrap;
}

export function pendingCompaction(d) {
  return h("div", { class: "timeline-divider pending-comp", html: `<span class="inner">${spinner()}正在压缩上下文 · <span class="tnum">${fmt.k(d.tokens)} / ${fmt.k(d.window)}</span> tokens</span>` });
}

export function runSummary(r) {
  const u = r.usage || {};
  const secs = Math.max(0, (r.updated_at || 0) - (r.created_at || 0));
  const reason = r.result?.reason || r.status_reason || "";
  return h("div", { class: "run-summary", html: `${statusBadge(r.status)}<span class="reason">${esc(reason)}</span>
    <span class="stats tnum"><span title="轮次">${icon("repeat", "sm")}${esc(r.result?.turns ?? "—")} 轮</span>
    <span title="输入（含缓存命中）/ 输出 token，各轮累计">${icon("arrowUp", "sm")}${fmt.k(fmt.tokIn(u))} ${icon("arrowDown", "sm")}${fmt.k(u.output_tokens)}</span>
    ${u.cache_read_tokens ? `<span title="缓存命中的输入 token（按缓存价计费）">缓存命中 ${fmt.k(u.cache_read_tokens)}</span>` : ""}
    <span title="费用">${icon("coins", "sm")}${fmt.money(u.cost_usd)}</span><span title="耗时">${icon("clock", "sm")}${fmt.dur(secs)}</span></span>` });
}

export function noticeItem(d) {
  const tone = d.level === "warn" || d.level === "warning" ? "warning" : d.level === "error" ? "danger" : "info";
  return callout(tone, d.text || d.code || "");
}

export function startPage({ info, templates, onPick, mode }) {
  const wrap = h("div", { class: "start" });
  wrap.innerHTML = `<h2>开始一个任务</h2>
    <p class="lead">描述你要完成的事情。Agent 会在工作区里读取文件、执行命令；任何需要确认的操作都会先在这里请求你的批准。</p>
    <div class="facts">
      <span class="badge outline">${icon("cpu")}${esc(info.provider)} / ${esc(info.model)}</span>
      <span class="badge outline" title="${esc(info.workspace)}">${icon("folder")}${esc(info.workspace.split("/").slice(-2).join("/"))}</span>
      <span class="badge outline">${icon("shield")}权限：${esc(mode.label)}</span>
      <span class="badge outline">${icon("wrench")}${info.tools.length} 个工具</span>
    </div>
    ${info.provider === "mock" ? `<div style="margin-top:16px">${callout("info", "当前使用内置演示模型（无需 API Key），下面每个示例都会完整走一遍对应的 harness 机制。").outerHTML}</div>` : ""}
    <div class="section-title">${info.provider === "mock" ? "演示场景" : "常用任务"}</div>`;
  const grid = h("div", { class: "templates" });
  for (const t of templates) {
    const b = h("button", { class: "template", type: "button", html: `<span class="ic">${icon(t.icon)}</span><span class="lbl">${esc(t.title)}</span><span class="desc">${esc(t.desc)}</span>` });
    b.onclick = () => onPick(t.prompt);
    grid.append(b);
  }
  wrap.append(grid);
  return wrap;
}
