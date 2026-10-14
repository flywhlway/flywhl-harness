// 列表页：运行记录、审批收件箱、工具目录。都是“只读视图 + 定时刷新”，不持有长连接。
import { api } from "../lib/api.js";
import { $, $$, debounce, esc, fmt, h } from "../lib/dom.js";
import { icon } from "../lib/icons.js";
import { ACTIVE, EFFECT, effectBadge, statusBadge, toolIcon } from "../lib/meta.js";
import { toast } from "../lib/ui.js";
import { approvalCard } from "./timeline.js";

function pageShell(root, { title, iconName }) {
  root.innerHTML = "";
  const header = h("header", { class: "page-header", html: `
    <button class="btn ghost icon menu-toggle" type="button" data-action="open-nav" aria-label="打开导航">${icon("menu")}</button>
    <div class="title">${icon(iconName, "lg")}<h1>${esc(title)}</h1></div><div class="actions" id="pg-actions"></div>` });
  const page = h("div", { class: "page" });
  const inner = h("div", { class: "page-inner" });
  page.append(inner);
  root.append(header, page);
  document.title = `${title} · flywhl harness`;
  return { header, inner, actions: $("#pg-actions", header) };
}

function poller(fn, ms) {
  let t = 0, alive = true;
  const tick = async () => { if (!alive) return; if (!document.hidden) await fn().catch(() => {}); t = setTimeout(tick, ms); };
  t = setTimeout(tick, ms);
  return () => { alive = false; clearTimeout(t); };
}

const refreshBtn = (onclick) => h("button", { class: "btn", type: "button", onclick, html: `${icon("refresh")}<span class="lbl-sm-hide">刷新</span>` });

// ====================================================================== 运行记录
const GROUPS = [
  { id: "all", label: "全部", match: () => true },
  { id: "active", label: "进行中", match: (r) => ACTIVE.has(r.status) },
  { id: "attention", label: "需要处理", match: (r) => ["waiting_human", "paused", "interrupted", "failed", "incomplete", "budget_exhausted", "loop_detected", "timed_out"].includes(r.status) },
  { id: "completed", label: "已完成", match: (r) => r.status === "completed" },
];
const SORTS = {
  updated_at: (r) => r.updated_at, cost: (r) => r.usage?.cost_usd || 0,
  tokens: (r) => fmt.tokTotal(r.usage), dur: (r) => (r.updated_at || 0) - (r.created_at || 0),
};

export function mountRuns(root, app) {
  const { inner, actions } = pageShell(root, { title: "运行记录", iconName: "activity" });
  const S = { runs: [], group: "all", q: "", children: false, sort: "updated_at", dir: -1, loaded: false };
  inner.innerHTML = `
    <div class="page-intro"><p>所有会话的运行历史。点击一行打开对应会话；暂停、中断或失败的运行可以在会话里继续或重试。</p></div>
    <div class="stat-cards" id="rn-stats"></div>
    <div class="filters">
      <div class="segmented" role="radiogroup" aria-label="按状态筛选" id="rn-groups"></div>
      <span class="grow"></span>
      <label class="check"><input type="checkbox" id="rn-children">包含子代理</label>
      <div class="search-input">${icon("search")}<label class="sr-only" for="rn-q">搜索任务或会话</label><input class="input" id="rn-q" placeholder="搜索任务或会话" type="search"></div>
    </div>
    <div class="table-card"><div class="table-scroll" id="rn-table"></div></div>`;
  actions.append(refreshBtn(() => load(true)));
  const groups = $("#rn-groups", inner);
  for (const g of GROUPS) {
    const b = h("button", { type: "button", role: "radio", "aria-checked": String(g.id === S.group) }, g.label);
    b.onclick = () => { S.group = g.id; $$("button", groups).forEach((x) => x.setAttribute("aria-checked", String(x === b))); renderTable(); };
    groups.append(b);
  }
  $("#rn-q", inner).oninput = debounce((e) => { S.q = e.target.value.trim().toLowerCase(); renderTable(); }, 150);
  $("#rn-children", inner).onchange = (e) => { S.children = e.target.checked; load(true); };

  async function load(manual = false) {
    try {
      S.runs = await api.get(`/api/runs?limit=500&children=${S.children}`);
      S.loaded = true;
      renderStats(); renderTable();
    } catch (e) { if (manual || !S.loaded) toast(e.message, { tone: "danger", title: "加载运行记录失败" }); }
  }

  function renderStats() {
    const r = S.runs;
    const cost = r.reduce((s, x) => s + (x.usage?.cost_usd || 0), 0);
    const tok = r.reduce((s, x) => s + fmt.tokTotal(x.usage), 0);
    const stat = (k, v, ic) => `<div class="stat"><span class="k">${icon(ic, "sm")}${k}</span><span class="v">${v}</span></div>`;
    $("#rn-stats", inner).innerHTML = stat("运行总数", fmt.int(r.length), "activity")
      + stat("进行中", fmt.int(r.filter((x) => ACTIVE.has(x.status)).length), "clock")
      + stat("需要处理", fmt.int(r.filter(GROUPS[2].match).length), "alert")
      + stat("Token 合计", fmt.k(tok), "database") + stat("费用合计", fmt.money(cost), "coins");
  }

  function renderTable() {
    const box = $("#rn-table", inner);
    const g = GROUPS.find((x) => x.id === S.group);
    let rows = S.runs.filter(g.match);
    if (S.q) rows = rows.filter((r) => ((r.prompt || "") + " " + (r.session_title || "") + " " + r.id).toLowerCase().includes(S.q));
    const key = SORTS[S.sort];
    rows.sort((a, b) => (key(a) - key(b)) * S.dir);
    if (!S.loaded) { box.innerHTML = `<div class="empty-state"><span class="spinner"></span></div>`; return; }
    if (!rows.length) {
      box.innerHTML = `<div class="empty-state"><span class="ic">${icon("activity", "lg")}</span><h3>${S.runs.length ? "没有符合条件的运行" : "还没有运行"}</h3><p>${S.runs.length ? "换一个筛选条件或搜索词试试。" : "在新会话里描述一个任务，运行记录会出现在这里。"}</p></div>`;
      return;
    }
    const th = (id, label, cls = "") => {
      const sorted = S.sort === id;
      return `<th class="${cls}" aria-sort="${sorted ? (S.dir < 0 ? "descending" : "ascending") : "none"}"><button type="button" data-sort="${id}">${label}${sorted ? icon(S.dir < 0 ? "chevronDown" : "chevronUp", "sm") : ""}</button></th>`;
    };
    box.innerHTML = `<table class="table responsive"><thead><tr><th>状态</th><th>任务</th><th class="num">尝试</th><th class="num">轮次</th>
      ${th("tokens", "Token", "num")}${th("cost", "费用", "num")}${th("dur", "耗时", "num")}${th("updated_at", "更新时间", "num")}</tr></thead>
      <tbody>${rows.map((r) => `<tr class="clickable" data-sid="${esc(r.session_id)}" tabindex="0" aria-label="打开会话：${esc(r.session_title || r.prompt || r.id)}">
        <td>${statusBadge(r.status)}</td>
        <td><div class="cell-main" title="${esc(r.prompt || "")}">${r.parent_run_id ? `<span class="badge square" style="margin-right:6px">${icon("bot")}子代理</span>` : ""}${esc(r.prompt || "（恢复执行）")}</div>
          <div class="cell-sub">${esc(r.session_title || "")} · <span class="mono">${esc(fmt.shortId(r.id))}</span></div></td>
        <td class="num" data-hide-sm>${r.attempt}</td><td class="num" data-hide-sm>${esc(r.result?.turns ?? "—")}</td>
        <td class="num">${fmt.k(fmt.tokTotal(r.usage))}</td>
        <td class="num" data-hide-sm>${fmt.money(r.usage?.cost_usd)}</td>
        <td class="num" data-hide-sm>${fmt.dur((r.updated_at || 0) - (r.created_at || 0))}</td>
        <td class="num" title="${esc(fmt.time(r.updated_at))}">${esc(fmt.ago(r.updated_at))}</td></tr>`).join("")}</tbody></table>`;
    $$("[data-sort]", box).forEach((b) => (b.onclick = () => {
      if (S.sort === b.dataset.sort) S.dir = -S.dir; else { S.sort = b.dataset.sort; S.dir = -1; }
      renderTable();
    }));
    $$("tr[data-sid]", box).forEach((tr) => {
      const open = () => app.navigate("#/s/" + tr.dataset.sid);
      tr.onclick = open;
      tr.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
    });
  }

  renderTable();
  load();
  const stop = poller(load, 5000);
  return { dispose: stop };
}

// ====================================================================== 审批收件箱
export function mountApprovals(root, app) {
  const { inner, actions } = pageShell(root, { title: "审批", iconName: "shieldCheck" });
  inner.innerHTML = `<div class="page-intro"><p>所有会话中等待人工确认的操作。批准、修改参数后批准或拒绝，结果会立即回到对应的运行中；超时未处理的审批会被自动拒绝。</p></div>
    <div class="approval-inbox" id="ap-list"></div>`;
  actions.append(refreshBtn(() => load(true)));
  const list = $("#ap-list", inner);
  let ids = "";
  async function load(manual = false) {
    let items;
    try { items = await api.get("/api/approvals"); } catch (e) { if (manual) toast(e.message, { tone: "danger" }); return; }
    const sig = items.map((a) => a.id).join(",");
    if (sig === ids && !manual) return;          // 没有变化就不重绘，避免打断正在编辑参数的审批人
    ids = sig;
    list.innerHTML = "";
    if (!items.length) {
      list.innerHTML = `<div class="empty-state"><span class="ic">${icon("shieldCheck", "lg")}</span><h3>没有待审批的操作</h3><p>Agent 需要执行写入、命令或外部操作时，会在这里请求你的确认。</p></div>`;
      return;
    }
    for (const a of items) {
      const card = approvalCard(a, { sid: null, showSource: true, onResolved: () => { card.remove(); app.refreshApprovals(); load(true); } });
      const link = h("a", { class: "btn ghost sm", href: "#/s/" + a.session_id, html: `${icon("external", "sm")}打开会话` });
      $(".approval-foot", card).prepend(link);
      list.append(card);
    }
  }
  load(true);
  const stop = poller(load, 3000);
  return { dispose: stop };
}

// ====================================================================== 工具目录
export function mountTools(root, app) {
  const { inner } = pageShell(root, { title: "工具", iconName: "wrench" });
  const tools = app.info.tools;
  const count = (fx) => tools.filter((t) => t.effect === fx).length;
  const S = { fx: "all", q: "" };
  inner.innerHTML = `
    <div class="page-intro"><p>Agent 可以调用的工具及其副作用等级。等级决定并发、自动重试、默认审批与取消方式：只读工具自动执行，写入与外部操作按权限模式请求确认。</p></div>
    <div class="stat-cards">
      ${Object.entries(EFFECT).map(([k, v]) => `<div class="stat"><span class="k">${effectBadge(k)}</span><span class="v">${count(k)}</span><span class="muted" style="font-size:var(--fs-xs)">${esc(v.hint)}</span></div>`).join("")}
    </div>
    <div class="filters">
      <div class="segmented" role="radiogroup" aria-label="按副作用筛选" id="tl-fx"></div><span class="grow"></span>
      <div class="search-input">${icon("search")}<label class="sr-only" for="tl-q">搜索工具</label><input class="input" id="tl-q" type="search" placeholder="搜索名称或说明"></div>
    </div>
    <div class="table-card"><div class="table-scroll" id="tl-table"></div></div>`;
  const seg = $("#tl-fx", inner);
  for (const [id, label] of [["all", "全部"], ["read", "只读"], ["write", "写入"], ["external", "外部"]]) {
    const b = h("button", { type: "button", role: "radio", "aria-checked": String(id === S.fx) }, label);
    b.onclick = () => { S.fx = id; $$("button", seg).forEach((x) => x.setAttribute("aria-checked", String(x === b))); render(); };
    seg.append(b);
  }
  $("#tl-q", inner).oninput = debounce((e) => { S.q = e.target.value.trim().toLowerCase(); render(); }, 120);
  function render() {
    const rows = tools.filter((t) => (S.fx === "all" || t.effect === S.fx) && (!S.q || (t.name + " " + t.description).toLowerCase().includes(S.q)));
    $("#tl-table", inner).innerHTML = rows.length ? `<table class="table responsive"><thead><tr><th>工具</th><th>副作用</th><th>说明</th></tr></thead><tbody>${rows.map((t) => `<tr>
      <td style="white-space:nowrap"><span style="display:inline-flex;gap:8px;align-items:center">${icon(toolIcon(t.name))}<span class="mono" style="font-weight:600">${esc(t.name)}</span>${t.name.startsWith("mcp__") ? '<span class="badge square info">MCP</span>' : ""}</span></td>
      <td>${effectBadge(t.effect)}</td><td style="color:var(--text-2);line-height:1.6;min-width:280px">${esc(t.description)}</td></tr>`).join("")}</tbody></table>`
      : `<div class="empty-state"><span class="ic">${icon("search", "lg")}</span><h3>没有匹配的工具</h3></div>`;
  }
  render();
  return { dispose() {} };
}
