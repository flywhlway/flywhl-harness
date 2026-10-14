// 检查器：观察一次 Run 的内部——概览（状态、预算、状态流转）、上下文、事件流、历史（分支与快照）。
import { api } from "../lib/api.js";
import { $, $$, copyText, debounce, esc, fmt, h, store } from "../lib/dom.js";
import { icon } from "../lib/icons.js";
import { RUN_STATUS, TERMINAL, eventGroup, statusBadge } from "../lib/meta.js";
import { confirmDialog, toast } from "../lib/ui.js";

const TABS = [
  { id: "overview", label: "概览", icon: "activity" },
  { id: "context", label: "上下文", icon: "database" },
  { id: "events", label: "事件", icon: "listChecks" },
  { id: "history", label: "历史", icon: "history" },
];

export function createInspector(V, app) {
  const el = h("aside", { class: "inspector", "aria-label": "检查器" });
  const tablist = h("div", { class: "tabs", role: "tablist", "aria-label": "检查器视图" });
  const panels = {};
  let tab = store.get("flywhl-tab", "overview");
  if (!TABS.some((t) => t.id === tab)) tab = "overview";

  for (const t of TABS) {
    const b = h("button", { role: "tab", id: "tab-" + t.id, type: "button", "aria-controls": "panel-" + t.id, html: `${icon(t.icon, "sm")}${t.label}` });
    b.onclick = () => setTab(t.id);
    tablist.append(b);
    panels[t.id] = h("section", { class: "tab-panel", role: "tabpanel", id: "panel-" + t.id, "aria-labelledby": "tab-" + t.id, tabindex: "0" });
  }
  tablist.addEventListener("keydown", (e) => {     // 方向键在标签之间移动（WAI-ARIA tabs 模式）
    const i = TABS.findIndex((t) => t.id === tab);
    const next = e.key === "ArrowRight" ? (i + 1) % TABS.length : e.key === "ArrowLeft" ? (i - 1 + TABS.length) % TABS.length : e.key === "Home" ? 0 : e.key === "End" ? TABS.length - 1 : -1;
    if (next < 0) return;
    e.preventDefault();
    setTab(TABS[next].id);
    $("#tab-" + TABS[next].id, tablist).focus();
  });
  el.append(tablist, ...Object.values(panels));

  function setTab(id) {
    tab = id;
    store.set("flywhl-tab", id);
    for (const t of TABS) {
      const b = $("#tab-" + t.id, tablist);
      b.setAttribute("aria-selected", String(t.id === id));
      b.tabIndex = t.id === id ? 0 : -1;
      panels[t.id].hidden = t.id !== id;
    }
    render();
  }

  function render() {
    if (tab === "overview") renderOverview(panels.overview, V, app);
    else if (tab === "context") renderContext(panels.context, V, app);
    else if (tab === "events") renderEvents(panels.events, V, app);
    else renderHistory(panels.history, V, app);
  }

  setTab(tab);
  return {
    el, render,
    get tab() { return tab; },
    onEvent(ev) {
      if (tab === "events") appendEventRow(panels.events, ev, V);
      else if (tab === "overview" && ["status", "usage", "run_end"].includes(ev.type)) render();
      else if (tab === "context" && ["compaction_end", "run_end"].includes(ev.type)) render();
    },
  };
}

// ====================================================================== 概览
function meter(label, val, max, { text, tone, marks = [] } = {}) {
  const pct = max ? Math.min(100, (val / max) * 100) : 0;
  const cls = tone || (pct >= 90 ? "hot" : pct >= 70 ? "warn" : "");
  const m = marks.map(([v, c]) => `<span class="mark ${c}" style="left:${Math.min(100, (v / max) * 100)}%"></span>`).join("");
  return `<div class="meter" role="group" aria-label="${esc(label)}"><div class="top"><span>${esc(label)}</span><span class="v">${esc(text ?? `${fmt.int(val)}${max ? " / " + fmt.int(max) : ""}`)}</span></div>
    <div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(pct)}" aria-label="${esc(label)}"><i class="${cls}" style="width:${pct}%"></i>${m}</div></div>`;
}

const FSM = {
  created: [6, 8], queued: [6, 40], running: [92, 40], waiting_human: [92, 8], paused: [6, 98], interrupted: [92, 98],
  cancelling: [92, 156], completed: [214, 8], incomplete: [214, 32], failed: [214, 56], budget_exhausted: [214, 80],
  loop_detected: [214, 104], timed_out: [214, 128], cancelled: [214, 156],
};
const FSM_EDGES = [["created", "queued"], ["queued", "running"], ["running", "waiting_human"], ["waiting_human", "running"],
  ["running", "paused"], ["paused", "queued"], ["running", "interrupted"], ["interrupted", "queued"], ["running", "cancelling"],
  ["cancelling", "cancelled"], ["running", "completed"], ["running", "incomplete"], ["running", "failed"],
  ["running", "budget_exhausted"], ["running", "loop_detected"], ["running", "timed_out"], ["failed", "queued"]];

function fsmSvg(cur, visited, hot) {
  const W = 104, H = 18;
  const c = (s) => [FSM[s][0] + W / 2, FSM[s][1] + H / 2];
  const edges = FSM_EDGES.map(([a, b]) => {
    const [x1, y1] = c(a), [x2, y2] = c(b);
    let sx = x1, sy = y1, ex = x2, ey = y2;
    if (Math.abs(x2 - x1) > Math.abs(y2 - y1)) { sx = x1 + Math.sign(x2 - x1) * W / 2; ex = x2 - Math.sign(x2 - x1) * W / 2; }
    else { sy = y1 + Math.sign(y2 - y1) * H / 2; ey = y2 - Math.sign(y2 - y1) * H / 2; }
    if (a === "waiting_human" && b === "running") { sx += 14; ex += 14; }
    if (a === "running" && b === "waiting_human") { sx -= 14; ex -= 14; }
    return `<path class="ed${hot && hot[0] === a && hot[1] === b ? " hot" : ""}" d="M${sx} ${sy} L${ex} ${ey}"/>`;
  }).join("");
  const nodes = Object.keys(FSM).map((s) => `<g class="${s === cur ? "cur" : visited.has(s) ? "visited" : ""}"><rect x="${FSM[s][0]}" y="${FSM[s][1]}" width="${W}" height="${H}" rx="4"/><text x="${FSM[s][0] + W / 2}" y="${FSM[s][1] + H / 2 + 0.5}">${s}</text></g>`).join("");
  return `<svg class="fsm" viewBox="0 0 324 180" role="img" aria-label="Run 状态机，当前状态 ${esc(cur || "无")}"><defs><marker id="fsm-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="5" markerHeight="5" orient="auto"><path d="M0 0 L8 4 L0 8 z" fill="var(--text-3)"/></marker></defs>${edges}${nodes}</svg>`;
}

async function loadTrail(V) {
  // 页面打开之前就结束的 Run：从轨迹里取出状态迁移事件（只取一次）
  const r = V.run;
  if (!r || V.trailFor === r.id || V.loadingTrail) return false;
  V.loadingTrail = true;
  try {
    const t = await api.get(`/api/runs/${r.id}/trajectory`);
    V.trail = (t.events || []).filter((e) => e.type === "status").map((e) => ({ ...e.data, ts: e.ts }));
    V.trailFor = r.id;
    return true;
  } catch { return false; } finally { V.loadingTrail = false; }
}

function renderOverview(p, V, app) {
  const r = V.run;
  if (!V.sid) { p.innerHTML = emptyHint("尚未开始", "发送第一条任务后，这里会实时显示运行状态、预算与状态流转。"); return; }
  if (r && V.trailFor !== r.id && !V.running) loadTrail(V).then((ok) => ok && renderOverview(p, V, app));
  const u = V.usage || {};
  const usage = u.usage || r?.usage || {};
  const budget = app.info?.budget || {};
  const visited = new Set(V.trail.flatMap((x) => [x.from, x.to]));
  const last = V.trail[V.trail.length - 1];
  const runs = V.detail?.runs || [];
  const tokens = fmt.tokTotal(usage);
  const maxTok = u.max_total_tokens || budget.max_total_tokens || 0;
  const maxCost = u.max_cost_usd || budget.max_cost_usd || 0;

  p.innerHTML = `
    <div class="panel-section">
      <div class="panel-title">当前运行${r ? `<span class="aside">${statusBadge(r.status)}</span>` : ""}</div>
      ${r ? `<dl class="kv">
        <dt>Run ID</dt><dd><span class="mono">${esc(fmt.shortId(r.id))}</span><button class="btn ghost icon sm" type="button" data-copy="${esc(r.id)}" aria-label="复制 Run ID" data-tip="复制完整 Run ID">${icon("copy", "sm")}</button></dd>
        <dt>尝试</dt><dd class="tnum">第 ${r.attempt} 次</dd>
        <dt>版本</dt><dd class="tnum">v${r.version} <span class="muted" style="font-size:var(--fs-xs)">每次状态迁移 +1</span></dd>
        <dt>Worker</dt><dd><span class="mono">${esc(r.worker_id || "—")}</span></dd>
        <dt>心跳</dt><dd>${esc(r.heartbeat_at ? fmt.ago(r.heartbeat_at) : "—")}</dd>
        <dt>创建于</dt><dd>${esc(fmt.time(r.created_at))}</dd></dl>` : `<p class="muted" style="font-size:var(--fs-sm)">这个会话还没有运行。</p>`}
    </div>
    <div class="panel-section">
      <div class="panel-title">预算</div>
      ${meter("轮次", u.turns ?? r?.result?.turns ?? 0, u.max_turns ?? budget.max_turns)}
      ${meter("工具调用", u.tool_calls ?? 0, u.max_tool_calls ?? budget.max_tool_calls)}
      ${meter("Token", tokens, maxTok, { text: `${fmt.k(fmt.tokIn(usage))} 入${usage.cache_read_tokens ? `（缓存 ${fmt.k(usage.cache_read_tokens)}）` : ""} · ${fmt.k(usage.output_tokens)} 出${maxTok ? " / " + fmt.k(maxTok) : ""}` })}
      ${meter("费用", usage.cost_usd || 0, maxCost, { text: `${fmt.money(usage.cost_usd)}${maxCost ? " / " + fmt.money(maxCost) : ""}` })}
    </div>
    <div class="panel-section">
      <div class="panel-title">状态流转<span class="aside">CAS 迁移，含原因</span></div>
      ${V.trail.length ? `<ol class="steps">${V.trail.map((x, i) => `<li class="${i === V.trail.length - 1 ? "cur" : ""}"><span class="node" aria-hidden="true"></span>
        <span class="t">${statusBadge(x.from, { square: true, still: true })}${icon("chevronRight", "sm")}${statusBadge(x.to, { square: true, still: i < V.trail.length - 1 || !V.running })}${x.version ? `<span class="muted tnum" style="font-size:11px">v${x.version}</span>` : ""}</span>
        ${x.reason ? `<span class="why">${esc(x.reason)}</span>` : ""}</li>`).join("")}</ol>`
        : `<p class="muted" style="font-size:var(--fs-sm)">${V.loadingTrail ? "正在加载…" : "暂无状态迁移记录"}</p>`}
      <details class="disclosure"><summary>${icon("chevronRight", "sm chev")}<span>状态机全图</span><span class="aside">14 个状态</span></summary>
        <div class="content">${fsmSvg(r?.status, visited, last ? [last.from, last.to] : null)}</div></details>
    </div>
    <div class="panel-section">
      <div class="panel-title">本会话的运行<span class="aside tnum">${runs.length} 次</span></div>
      <ul class="list">${runs.map((x) => `<li class="list-row">${statusBadge(x.status)}<span class="p" title="${esc(x.prompt || "")}">${esc(x.prompt || "（恢复执行）")}</span><span class="muted tnum" style="font-size:11px">${esc(fmt.ago(x.updated_at))}</span>
        <span class="row-actions">
          ${x.status === "failed" ? `<button class="btn sm" type="button" data-resume="${esc(x.id)}">${icon("retry", "sm")}重试</button>` : ""}
          ${["paused", "interrupted"].includes(x.status) ? `<button class="btn sm" type="button" data-resume="${esc(x.id)}">${icon("play", "sm")}继续运行</button>` : ""}
          ${TERMINAL.has(x.status) ? `<button class="btn sm" type="button" data-replay="${esc(x.id)}" data-tip="用录制的模型输出重放，不调用模型、不执行任何副作用">${icon("rewind", "sm")}确定性回放</button>` : ""}
          <a class="btn sm ghost" href="/api/runs/${encodeURIComponent(x.id)}/trajectory" target="_blank" rel="noopener">${icon("external", "sm")}轨迹 JSON</a>
        </span></li>`).join("") || `<li class="muted" style="font-size:var(--fs-sm);padding:8px 10px">还没有运行</li>`}</ul>
    </div>`;

  $$("[data-copy]", p).forEach((b) => (b.onclick = async () => { if (await copyText(b.dataset.copy)) toast("已复制 Run ID", { tone: "success", timeout: 1800 }); }));
  $$("[data-resume]", p).forEach((b) => (b.onclick = () => app.resumeRun(runs.find((x) => x.id === b.dataset.resume))));
  $$("[data-replay]", p).forEach((b) => (b.onclick = async () => {
    b.disabled = true;
    const html = b.innerHTML;
    b.innerHTML = `<span class="spinner" style="width:12px;height:12px;border-width:1.5px"></span>回放中`;
    try {
      const rep = await api.post(`/api/runs/${b.dataset.replay}/replay`, {}, { timeout: 120000 });
      if (rep.same_tool_sequence && rep.same_final_text) toast("工具调用序列与最终答案都与原始运行一致；回放过程中没有执行任何副作用。", { tone: "success", title: "回放一致" });
      else toast(rep.divergence ? `第 ${rep.divergence.step} 次模型调用时上下文与录制不一致，说明 harness 的行为发生了变化。` : "回放结果与原始运行不同。", { tone: "warning", title: "回放出现差异", timeout: 7000 });
    } catch (e) { toast(e.message, { tone: "danger", title: "回放失败" }); }
    b.disabled = false; b.innerHTML = html;
  }));
}

// ====================================================================== 上下文
async function renderContext(p, V) {
  if (!V.sid) { p.innerHTML = emptyHint("尚未开始", "会话开始后，这里会显示模型每次调用时看到的上下文与水位。"); return; }
  if (!p.childElementCount) p.innerHTML = skeleton();
  let c;
  try { c = await api.get(`/api/sessions/${V.sid}/context`); } catch (e) { p.innerHTML = errorHint(e.message); return; }
  const st = c.stats;
  const max = Math.max(...c.items.map((i) => i.tokens), 1);
  const total = Math.max(1, c.system_tokens + c.tools_tokens + c.messages_tokens);
  const seg = (v, color) => `<i style="width:${(v / total) * 100}%;background:${color}"></i>`;
  p.innerHTML = `
    <div class="panel-section">
      <div class="panel-title">上下文水位<span class="aside">${st.source === "usage+estimate" ? "真实用量 + 增量估算" : "本地估算"}</span></div>
      ${meter(`${fmt.k(st.tokens)} / ${fmt.k(st.window)} tokens`, st.tokens, st.window, { text: `${(st.ratio * 100).toFixed(1)}%`, marks: [[st.clear_at, "clear"], [st.compact_at, "compact"]] })}
      <div class="legend"><span><i style="background:var(--warning)"></i>清理旧结果 ${fmt.k(st.clear_at)}</span><span><i style="background:var(--danger)"></i>触发压缩 ${fmt.k(st.compact_at)}</span></div>
    </div>
    <div class="panel-section">
      <div class="panel-title">构成</div>
      <div class="stack-bar" role="img" aria-label="系统提示 ${fmt.k(c.system_tokens)}，工具定义 ${fmt.k(c.tools_tokens)}，消息 ${fmt.k(c.messages_tokens)}">
        ${seg(c.system_tokens, "var(--info)")}${seg(c.tools_tokens, "var(--warning)")}${seg(c.messages_tokens, "var(--brand)")}</div>
      <div class="legend"><span><i style="background:var(--info)"></i>系统提示 ${fmt.k(c.system_tokens)}</span><span><i style="background:var(--warning)"></i>工具定义 ${fmt.k(c.tools_tokens)}</span><span><i style="background:var(--brand)"></i>消息 ${fmt.k(c.messages_tokens)} · ${c.items.length} 条</span></div>
      <div class="toolbar" style="margin-top:4px"><button class="btn sm" type="button" id="compact-btn" ${V.running ? "disabled" : ""} data-tip="${V.running ? "运行中不能手动压缩" : "把较早的历史改写为结构化摘要"}">${icon("database", "sm")}立即压缩</button></div>
    </div>
    ${c.compaction ? `<details class="disclosure"><summary>${icon("chevronRight", "sm chev")}<span>当前生效的压缩摘要</span><span class="aside">压缩前 ${fmt.k(c.compaction.tokens_before)}</span></summary><div class="content"><pre class="plain" id="comp-summary"></pre></div></details>` : ""}
    <div class="panel-section">
      <div class="panel-title">模型此刻看到的消息<span class="aside">按 token 占比</span></div>
      <ul class="ctx-items">${c.items.map((i) => `<li title="${esc(i.preview)}"><span class="r">${esc(i.kind && i.kind !== "prompt" ? i.kind : i.role)}</span><span class="pv">${esc(i.tool ? i.tool + "：" : "")}${esc(i.preview)}</span><span class="tk">${fmt.k(i.tokens)}</span><span class="sp" style="width:${(i.tokens / max) * 100}%"></span></li>`).join("")}</ul>
    </div>
    <details class="disclosure"><summary>${icon("chevronRight", "sm chev")}<span>系统提示词</span><span class="aside">会话创建时冻结</span></summary><div class="content"><pre class="plain" id="sys-prompt"></pre></div></details>`;
  if (c.compaction) $("#comp-summary", p).textContent = c.compaction.summary;
  $("#sys-prompt", p).textContent = c.system_prompt;
  const btn = $("#compact-btn", p);
  btn.onclick = async () => {
    btn.disabled = true;
    try {
      const r = await api.post(`/api/sessions/${V.sid}/compact`, {}, { timeout: 120000 });
      toast(r.ok ? `${fmt.k(r.result.tokens_before)} → ${fmt.k(r.result.tokens_after)} tokens` : "没有可以压缩的历史", { tone: r.ok ? "success" : "info", title: r.ok ? "压缩完成" : "" });
      await V.reload();
      renderContext(p, V);
    } catch (e) { toast(e.message, { tone: "danger" }); btn.disabled = false; }
  };
}

// ====================================================================== 事件
const GROUP_LABELS = { all: "全部类型", run: "运行与回合", msg: "消息", tool: "工具", policy: "审批", ctx: "上下文", other: "通知与错误" };

function eventDesc(ev) {
  const d = ev.data || {};
  switch (ev.type) {
    case "status": return `${d.from} → ${d.to}${d.reason ? " · " + d.reason : ""}`;
    case "message_update": return `${d.kind} ${JSON.stringify(d.delta ?? d.name ?? "").slice(0, 60)}`;
    case "message_end": return `${d.message?.role || ""} ${d.message?.meta?.kind || ""}`;
    case "tool_start": return `${d.name} [${d.effect}]`;
    case "tool_end": return `${d.name} → ${d.status}${d.duration_ms != null ? " · " + d.duration_ms + "ms" : ""}`;
    case "tool_update": return Object.keys(d.update || {}).join(", ");
    case "run_end": return `${d.status} · ${d.reason || ""}`;
    case "usage": return `第 ${d.turns} 轮 · 工具 ${d.tool_calls}`;
    case "notice": return d.text || d.code || "";
    case "approval_request": return `${d.tool} · ${d.summary || ""}`;
    case "approval_resolved": return d.approved ? "已批准" : "已拒绝";
    default: return Object.keys(d).slice(0, 4).join(", ");
  }
}

function eventVisible(ev, V) {
  if (ev.type === "__gap") return true;
  if (!V.showDeltas && (ev.type === "message_update" || ev.type === "tool_update")) return false;
  if (V.evFilter !== "all" && eventGroup(ev.type) !== V.evFilter) return false;
  if (V.evQuery && !(ev.type + " " + eventDesc(ev)).toLowerCase().includes(V.evQuery)) return false;
  return true;
}

function renderEvents(p, V) {
  p.innerHTML = `
    <div class="panel-section">
      <div class="toolbar">
        <label class="sr-only" for="ev-filter">按类型筛选</label>
        <select class="select" id="ev-filter" style="height:28px;font-size:var(--fs-xs)">${Object.entries(GROUP_LABELS).map(([k, v]) => `<option value="${k}"${V.evFilter === k ? " selected" : ""}>${v}</option>`).join("")}</select>
        <label class="sr-only" for="ev-q">搜索事件</label>
        <input class="input" id="ev-q" placeholder="搜索…" value="${esc(V.evQuery)}" style="height:28px;font-size:var(--fs-xs);flex:1;min-width:80px">
      </div>
      <div class="toolbar">
        <label class="check"><input type="checkbox" id="ev-deltas" ${V.showDeltas ? "checked" : ""}>显示增量事件</label>
        <span class="grow"></span>
        <span class="muted tnum" style="font-size:var(--fs-xs)">最新序号 <b id="ev-seq">${V.lastSeq}</b></span>
      </div>
      <div class="toolbar">
        <button class="btn sm" type="button" id="ev-drop" ${V.es ? "" : "disabled"} data-tip="主动断开事件流，2 秒后凭最后序号续传（Run 在服务端继续执行）">${icon("refresh", "sm")}模拟断线重连</button>
      </div>
    </div>
    <ul class="event-list" id="ev-list" aria-label="事件流"></ul>`;
  $("#ev-filter", p).onchange = (e) => { V.evFilter = e.target.value; fillEvents(p, V); };
  $("#ev-q", p).oninput = debounce((e) => { V.evQuery = e.target.value.trim().toLowerCase(); fillEvents(p, V); }, 150);
  $("#ev-deltas", p).onchange = (e) => { V.showDeltas = e.target.checked; fillEvents(p, V); };
  $("#ev-drop", p).onclick = () => V.simulateDrop();
  fillEvents(p, V);
}

function fillEvents(p, V) {
  const list = $("#ev-list", p);
  if (!list) return;
  list.innerHTML = "";
  const evs = V.events.filter((e) => eventVisible(e, V)).slice(-500);
  if (!evs.length) list.append(h("li", { class: "gap", style: "color:var(--text-3)" }, V.events.length ? "没有符合条件的事件" : "打开页面后收到的实时事件会显示在这里"));
  for (const ev of evs) list.append(eventRow(ev));
  p.scrollTop = p.scrollHeight;
}

function eventRow(ev) {
  if (ev.type === "__gap") return h("li", { class: "gap", html: `${icon("refresh", "sm")} ${esc(ev.text)}` });
  const li = h("li");
  const b = h("button", { class: "ev", type: "button", "aria-expanded": "false", html: `<span class="q">${ev.seq}</span><span class="ty c-${eventGroup(ev.type)}">${esc(ev.type)}</span><span class="ds">${esc(eventDesc(ev))}</span>` });
  b.onclick = () => {
    const pre = $("pre", li);
    b.setAttribute("aria-expanded", String(!pre));
    if (pre) pre.remove(); else { const x = h("pre"); x.textContent = JSON.stringify(ev, null, 2); li.append(x); }
  };
  li.append(b);
  return li;
}

export function appendEventRow(p, ev, V) {
  const seq = $("#ev-seq", p);
  if (seq) seq.textContent = V.lastSeq;
  const list = $("#ev-list", p);
  if (!list || !eventVisible(ev, V)) return;
  list.querySelector("li.gap:only-child")?.remove();
  const near = p.scrollHeight - p.scrollTop - p.clientHeight < 120;
  list.append(eventRow(ev));
  if (list.childElementCount > 600) list.firstElementChild.remove();
  if (near) p.scrollTop = p.scrollHeight;
}

// ====================================================================== 历史：分支与快照
async function renderHistory(p, V, app) {
  if (!V.sid) { p.innerHTML = emptyHint("尚未开始", "会话的分支历史与工作区快照会显示在这里。"); return; }
  if (!p.childElementCount) p.innerHTML = skeleton();
  let tree, snaps;
  try { [tree, snaps] = await Promise.all([api.get(`/api/sessions/${V.sid}/tree`), api.get(`/api/sessions/${V.sid}/snapshots`)]); }
  catch (e) { p.innerHTML = errorHint(e.message); return; }
  const byId = Object.fromEntries(tree.map((n) => [n.id, n]));
  const depth = (n) => { let d = 0, q = n.parent_id; while (q && byId[q]) { d++; q = byId[q].parent_id; } return d; };
  const kids = {};
  for (const n of tree) (kids[n.parent_id || "root"] ||= []).push(n);
  const forks = new Set(Object.entries(kids).filter(([, v]) => v.length > 1).map(([k]) => k));
  p.innerHTML = `
    <div class="panel-section">
      <div class="panel-title">会话分支<span class="aside">历史只追加；切换分支只移动 head</span></div>
      <div class="toolbar"><button class="btn sm" type="button" id="branch-root">${icon("branch", "sm")}从开头新建分支</button></div>
      <ul class="tree">${tree.map((n) => `<li class="${n.on_path ? "on" : ""} ${n.head ? "head" : ""}" style="padding-left:${Math.min(depth(n), 8) * 4 + 6}px">
        <span aria-hidden="true">${n.head ? icon("commit", "sm") : forks.has(n.parent_id) ? icon("branch", "sm") : ""}</span>
        <span class="lb" title="${esc(n.label)}">${n.head ? '<span class="sr-only">当前位置：</span>' : ""}${esc(n.label || n.kind)}</span>
        <button class="btn ghost sm" type="button" data-branch="${esc(n.id)}" aria-label="从这里创建分支">从此分支</button></li>`).join("") || `<li>暂无条目</li>`}</ul>
    </div>
    <div class="panel-section">
      <div class="panel-title">工作区快照<span class="aside">写操作前自动创建</span></div>
      <ul class="list">${snaps.map((s) => `<li class="list-row"><span class="badge mono square">${esc(s.sha.slice(0, 8))}</span><span class="p" title="${esc(s.message)}">${esc(s.message)}</span><span class="muted" style="font-size:11px">${esc(fmt.ago(s.ts))}</span>
        <span class="row-actions"><button class="btn sm" type="button" data-rollback="${esc(s.sha)}">${icon("undo", "sm")}回滚到此</button></span></li>`).join("") || `<li class="muted" style="font-size:var(--fs-sm);padding:8px 10px">暂无快照（只有写操作才会创建）</li>`}</ul>
    </div>`;
  const branch = async (entryId) => {
    try {
      await api.post(`/api/sessions/${V.sid}/branch`, { entry_id: entryId });
      toast("下一条输入将从这里继续，旧分支完整保留。", { tone: "success", title: "已切换分支" });
      await V.reload();
      renderHistory(p, V, app);
    } catch (e) { toast(e.message, { tone: "danger" }); }
  };
  $("#branch-root", p).onclick = () => branch(null);
  $$("[data-branch]", p).forEach((b) => (b.onclick = () => branch(b.dataset.branch)));
  $$("[data-rollback]", p).forEach((b) => (b.onclick = async () => {
    const ok = await confirmDialog({
      title: "回滚工作区？", desc: `工作区文件将恢复到快照 ${b.dataset.rollback.slice(0, 8)} 时的状态，快照之后新增的文件会被删除。会话历史不受影响。`,
      confirmText: "回滚", danger: true,
    });
    if (!ok) return;
    try {
      const r = await api.post(`/api/sessions/${V.sid}/rollback`, { sha: b.dataset.rollback });
      toast(`影响 ${r.changed.length} 个文件`, { tone: "success", title: "已回滚" });
      renderHistory(p, V, app);
    } catch (e) { toast(e.message, { tone: "danger", title: "回滚失败" }); }
  }));
}

// ====================================================================== 小部件
const emptyHint = (t, d) => `<div class="empty-state"><span class="ic">${icon("info", "lg")}</span><h3>${esc(t)}</h3><p>${esc(d)}</p></div>`;
const errorHint = (msg) => `<div class="callout danger"><span class="ic">${icon("xCircle")}</span><div class="callout-text">加载失败：${esc(msg)}</div></div>`;
const skeleton = () => `<p class="muted" style="font-size:var(--fs-sm);display:flex;gap:8px;align-items:center"><span class="spinner"></span>加载中…</p>`;
export { RUN_STATUS };
