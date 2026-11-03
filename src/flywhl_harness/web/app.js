// flywhl Agent 控制台入口：应用外壳、路由、侧边栏、全局快捷键与命令面板。
// 页面只是事件流的一个消费者：会话视图订阅 SSE，列表页定时刷新，所有状态以服务端为准。
import { api } from "./lib/api.js";
import { $, copyText, esc, fmt, h, isTyping, modKey, store } from "./lib/dom.js";
import { icon } from "./lib/icons.js";
import { MODES, RUN_STATUS } from "./lib/meta.js";
import { announce, dialog, flashCopied, installTooltips, notify, openMenu, openPalette, promptDialog, setTitleCount, toast } from "./lib/ui.js";
import { mountApprovals, mountRuns, mountTools } from "./views/pages.js";
import { mountSession } from "./views/session.js";
import { mountSettings } from "./views/settings.js";

const app = {
  info: null,
  toolFx: {},
  sessions: [],
  archived: [],
  showArchived: false,
  approvals: 0,
  view: null,
  prefs: new Proxy(store.get("flywhl-prefs", {}), { set(t, k, v) { t[k] = v; store.set("flywhl-prefs", t); return true; } }),
  navigate(hash, state = {}) {
    navState = state;
    if (location.hash === hash) route(); else location.hash = hash;
  },
  refreshSessions: () => loadSessions(),
  refreshApprovals: () => loadApprovals(),
  renameSession: (sid, title) => renameSession(sid, title),
  exportSession: (sid) => exportSession(sid),
  archiveSession: (sid, archived) => archiveSession(sid, archived),
  notify: (title, opts) => { if (app.prefs.notify) notify(title, opts); },
};
window.flywhl = app;   // 便于在浏览器控制台里观察状态（学习与调试用）
let navState = {};

// ====================================================================== 主题
const THEMES = [{ id: "system", label: "跟随系统", icon: "monitor" }, { id: "light", label: "浅色", icon: "sun" }, { id: "dark", label: "深色", icon: "moon" }];
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("flywhl-theme", t); } catch { /* 忽略 */ }
  renderFoot();
}

// ====================================================================== 侧边栏
function renderSidebarStatic() {
  $("#new-session").innerHTML = `${icon("compose")}新建会话`;
  $("#new-session").onclick = () => { closeNav(); app.navigate("#/"); };
  $("#open-palette").innerHTML = `${icon("search")}<span class="grow">搜索或执行命令</span><span class="kbd">${modKey}</span><span class="kbd">K</span>`;
  $("#open-palette").setAttribute("aria-label", `打开命令面板（${modKey} + K）`);
  $("#open-palette").onclick = palette;
  $("#scrim").onclick = closeNav;
  $("#toggle-archived").onclick = () => toggleArchived();
  // 会话条目右侧的“更多”按钮：列表每次刷新都会重绘，所以用事件委托
  $("#session-list").addEventListener("click", (e) => {
    const b = e.target.closest("[data-sess-menu]");
    if (!b) return;
    e.preventDefault();
    const s = [...app.sessions, ...app.archived].find((x) => x.id === b.dataset.sessMenu);
    if (s) sessionMenu(b, s);
  });
}

function renderNav() {
  const r = currentRoute();
  const items = [
    { href: "#/runs", label: "运行记录", icon: "activity", page: "runs" },
    { href: "#/approvals", label: "审批", icon: "shieldCheck", page: "approvals", count: app.approvals },
    { href: "#/tools", label: "工具", icon: "wrench", page: "tools" },
    { href: "#/settings", label: "环境与安全", icon: "shield", page: "settings" },
  ];
  $("#nav").innerHTML = items.map((it) => `<a href="${it.href}"${r.page === it.page ? ' aria-current="page"' : ""}>${icon(it.icon)}<span>${it.label}</span>${it.count ? `<span class="count" aria-label="${it.count} 个待审批">${it.count}</span>` : ""}</a>`).join("");
}

function renderSessions() {
  const r = currentRoute();
  const ul = $("#session-list");
  const list = app.showArchived ? app.archived : app.sessions;
  $("#recent-label").textContent = app.showArchived ? "已归档会话" : "最近会话";
  if (!list.length) {
    ul.innerHTML = `<li class="empty-hint">${app.showArchived ? "没有已归档的会话。" : "还没有会话。新建一个会话开始任务。"}</li>`;
    return;
  }
  ul.innerHTML = list.map((s) => {
    const st = s.active_run ? "running" : s.last_run?.status || "";
    const tone = { running: "brand pulse", waiting_human: "warning", completed: "success", failed: "danger", paused: "", interrupted: "warning" }[st] ?? "";
    const label = st ? RUN_STATUS[st]?.label || st : "未运行";
    return `<li><a href="#/s/${encodeURIComponent(s.id)}"${r.sid === s.id ? ' aria-current="page"' : ""} title="${esc(s.title)}">
      <span class="dot ${tone}" aria-hidden="true"></span><span class="t">${esc(s.title)}</span><span class="s">${esc(label)} · ${esc(fmt.ago(s.updated_at))}</span></a>
      <button class="sess-more" type="button" data-sess-menu="${esc(s.id)}" aria-haspopup="menu" aria-expanded="false" aria-label="会话操作：${esc(s.title)}">${icon("more", "sm")}</button></li>`;
  }).join("");
}

function renderFoot() {
  const foot = $("#sidebar-foot");
  if (!app.info) return;
  const cur = document.documentElement.dataset.theme || "system";
  foot.innerHTML = `
    <div class="row" title="${esc(app.info.workspace)}">${icon("folder", "sm")}<span class="v">${esc(app.info.workspace)}</span></div>
    <div class="row">${icon("cpu", "sm")}<span class="v">${esc(app.info.provider)} / ${esc(app.info.model)}</span></div>
    <div class="theme"><span>外观</span><div class="segmented" role="radiogroup" aria-label="外观主题">${THEMES.map((t) => `<button type="button" role="radio" aria-checked="${t.id === cur}" data-theme-id="${t.id}" aria-label="${t.label}" data-tip="${t.label}">${icon(t.icon, "sm")}</button>`).join("")}</div></div>`;
  foot.querySelectorAll("[data-theme-id]").forEach((b) => (b.onclick = () => applyTheme(b.dataset.themeId)));
}

const openNav = () => { $("#app").classList.add("nav-open"); $("#scrim").hidden = false; $("#sidebar").querySelector("a, button")?.focus(); };
function closeNav() { $("#app").classList.remove("nav-open"); $("#scrim").hidden = true; }

async function loadSessions() {
  try {
    app.sessions = await api.get("/api/sessions");
    if (app.showArchived) app.archived = await api.get("/api/sessions?archived=true");
    renderSessions();
  } catch { /* 侧边栏刷新失败不打扰用户 */ }
}

function toggleArchived(on = !app.showArchived) {
  app.showArchived = on;
  $("#toggle-archived").setAttribute("aria-pressed", String(on));
  renderSessions();
  loadSessions();
}

let seenApprovals = null;   // 已经见过的审批：只为新出现的发桌面通知（首次加载不通知）
async function loadApprovals() {
  let list;
  try { list = await api.get("/api/approvals"); } catch { return; }
  const n = list.length;
  if (n > app.approvals) announce(`有 ${n} 个操作等待审批`);   // 数量增加时播报完整的一句话，而不是单独的数字
  const fresh = seenApprovals ? list.filter((a) => !seenApprovals.has(a.id)) : [];
  seenApprovals = new Set(list.map((a) => a.id));
  if (fresh.length) {
    const a = fresh[0];
    app.notify(fresh.length > 1 ? `${fresh.length} 个操作等待审批` : "有操作等待你的审批", {
      body: `${a.session_title ? a.session_title + "：" : ""}${a.tool} · ${a.summary}`, tag: "flywhl-approval",
      onClick: () => app.navigate(a.session_id ? "#/s/" + a.session_id : "#/approvals"),
    });
  }
  setTitleCount(n);
  if (n !== app.approvals) { app.approvals = n; renderNav(); }
}

// ====================================================================== 会话操作（页头菜单、侧边栏菜单、命令面板共用）
const sidPath = (sid) => `/api/sessions/${encodeURIComponent(sid)}`;

async function renameSession(sid, current) {
  const title = await promptDialog({
    title: "重命名会话", label: "会话标题", value: current, multiline: false, confirmText: "保存",
    help: "只影响显示，不会发给模型。", validate: (v) => (v ? "" : "标题不能为空"),
  });
  if (title == null || title === current) return null;
  try {
    const r = await api.patch(sidPath(sid), { title });
    await loadSessions();
    app.view?.applyTitle?.(sid, r.title);
    return r.title;
  } catch (e) { toast(e.message, { tone: "danger", title: "重命名失败" }); return null; }
}

async function exportSession(sid) {
  try {
    await api.download(`${sidPath(sid)}/export`, `${sid}.md`);
    toast("当前分支的对话记录（含工具调用与结果）已保存为 Markdown。", { title: "导出完成", tone: "success" });
  } catch (e) { toast(e.message, { tone: "danger", title: "导出失败" }); }
}

async function archiveSession(sid, archived = true) {
  try { await api.patch(sidPath(sid), { archived }); }
  catch (e) { toast(e.message, { tone: "danger", title: archived ? "无法归档" : "无法取消归档" }); return false; }
  await loadSessions();
  app.view?.applyArchived?.(sid, archived);
  if (archived) {
    const wasOpen = currentRoute().sid === sid;   // 归档正在看的会话会回到开始页；撤销时再带回来
    toast("可以在侧边栏「已归档」里找回。", { title: "会话已归档", tone: "success",
      action: { label: "撤销", run: async () => { if (await archiveSession(sid, false) && wasOpen) app.navigate("#/s/" + sid); } } });
    if (wasOpen) app.navigate("#/");
  } else toast("会话回到了最近会话列表。", { title: "已取消归档", tone: "success" });
  return true;
}

function sessionMenu(anchor, s) {
  openMenu(anchor, [
    { label: "重命名…", icon: "pencil", onSelect: () => renameSession(s.id, s.title) },
    { label: "导出为 Markdown", icon: "download", onSelect: () => exportSession(s.id) },
    { label: "复制会话 ID", icon: "copy", onSelect: async () => { if (await copyText(s.id)) toast(s.id, { title: "已复制会话 ID", tone: "success" }); } },
    "sep",
    s.archived ? { label: "取消归档", icon: "archive", onSelect: () => archiveSession(s.id, false) }
      : { label: "归档", icon: "archive", disabled: !!s.active_run, onSelect: () => archiveSession(s.id, true) },
  ], { label: `会话操作：${s.title}` });
}

// ====================================================================== 路由
function currentRoute() {
  const p = location.hash.replace(/^#/, "") || "/";
  const m = p.match(/^\/s\/([^/?]+)/);
  if (m) return { page: "session", sid: decodeURIComponent(m[1]) };
  if (p.startsWith("/runs")) return { page: "runs" };
  if (p.startsWith("/approvals")) return { page: "approvals" };
  if (p.startsWith("/tools")) return { page: "tools" };
  if (p.startsWith("/settings")) return { page: "settings" };
  return { page: "new" };
}

function route() {
  const r = currentRoute();
  const state = navState;
  navState = {};
  app.view?.dispose?.();
  const main = $("#main");
  if (r.page === "session") app.view = mountSession(main, app, r.sid, state);
  else if (r.page === "runs") app.view = mountRuns(main, app);
  else if (r.page === "approvals") app.view = mountApprovals(main, app);
  else if (r.page === "tools") app.view = mountTools(main, app);
  else if (r.page === "settings") app.view = mountSettings(main, app);
  else app.view = mountSession(main, app, null);
  if (r.sid) store.set("flywhl-last-session", r.sid);
  renderNav();
  renderSessions();
  closeNav();
  if (state.focusMain) main.focus({ preventScroll: true });   // 路由切换后把焦点交给主内容区（屏幕阅读器从这里开始读）
}

// ====================================================================== 命令面板与快捷键
function palette() {
  openPalette(() => {
    const v = app.view;
    const cmds = [
      { group: "操作", label: "新建会话", icon: "compose", run: () => app.navigate("#/") },
      { group: "前往", label: "运行记录", icon: "activity", keywords: "runs history", run: () => app.navigate("#/runs") },
      { group: "前往", label: "审批", icon: "shieldCheck", hint: app.approvals ? `${app.approvals} 个待处理` : "", keywords: "approvals", run: () => app.navigate("#/approvals") },
      { group: "前往", label: "工具", icon: "wrench", keywords: "tools", run: () => app.navigate("#/tools") },
      { group: "前往", label: "环境与安全", icon: "shield", keywords: "settings security config sandbox token", run: () => app.navigate("#/settings") },
      { group: "前往", label: app.showArchived ? "显示最近会话" : "显示已归档会话", icon: "archive", keywords: "archived", run: () => toggleArchived() },
    ];
    if (v?.sid) {
      const s = app.sessions.find((x) => x.id === v.sid) || app.archived.find((x) => x.id === v.sid);
      cmds.push({ group: "当前会话", label: "显示或隐藏检查器", icon: "panel", hint: `${modKey} \\`, run: () => v.toggleInspector() });
      if (v.running) cmds.push({ group: "当前会话", label: "停止当前运行", icon: "stop", run: () => v.stop() });
      cmds.push({ group: "当前会话", label: "重命名会话", icon: "pencil", keywords: "rename title", run: () => v.rename() });
      cmds.push({ group: "当前会话", label: "导出为 Markdown", icon: "download", keywords: "export markdown", run: () => exportSession(v.sid) });
      if (!v.running && !s?.archived) cmds.push({ group: "当前会话", label: "归档会话", icon: "archive", keywords: "archive", run: () => archiveSession(v.sid, true) });
      for (const m of MODES) cmds.push({ group: "当前会话", label: `权限模式：${m.label}`, icon: m.icon, keywords: m.id, run: () => v.setMode(m.id) });
    }
    for (const t of THEMES) cmds.push({ group: "外观", label: `主题：${t.label}`, icon: t.icon, keywords: "theme", run: () => applyTheme(t.id) });
    cmds.push({ group: "帮助", label: "键盘快捷键", icon: "keyboard", keywords: "shortcut help", run: shortcutsHelp });
    for (const s of app.sessions.slice(0, 50)) cmds.push({ group: "会话", label: s.title, icon: "chat", hint: fmt.ago(s.updated_at), run: () => app.navigate("#/s/" + s.id) });
    return cmds;
  });
}

function shortcutsHelp() {
  const rows = [[`${modKey} K`, "打开命令面板"], ["/", "聚焦输入框"], ["Enter", "发送 / 插话"], ["Shift Enter", "换行"],
    ["↑ ↓", "输入框为空时翻阅发送过的任务"], ["A", "跳到第一个待审批操作（再按 Enter 批准）"],
    [`${modKey} \\`, "显示或隐藏检查器"], [`${modKey} Enter`, "对话框中确认"], ["Esc", "关闭菜单、对话框与面板"]];
  const body = h("div", { html: `<table class="table" style="font-size:var(--fs-sm)"><tbody>${rows.map(([k, d]) => `<tr><td style="width:40%">${k.split(" ").map((x) => `<span class="kbd">${esc(x)}</span>`).join(" ")}</td><td>${esc(d)}</td></tr>`).join("")}</tbody></table>` });
  dialog({ title: "键盘快捷键", iconName: "keyboard", body, confirmText: "知道了", hideCancel: true });
}

/** A：把焦点移到第一个待审批操作的“批准”按钮上。只移动焦点、不替人批准——批准仍要再按一次 Enter */
function focusApproval() {
  const btn = $('.approval:not(.resolving) [data-act="approve"]');
  if (btn) {
    btn.scrollIntoView({ block: "center", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    btn.focus({ preventScroll: true });
    announce("已定位到待审批操作，按 Enter 批准，Tab 切换到其他选项");
  } else if (app.approvals) app.navigate("#/approvals");
  else toast("当前没有等待审批的操作。");
}

function installShortcuts() {
  document.addEventListener("keydown", (e) => {
    const mod = e.metaKey || e.ctrlKey;
    if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); palette(); }
    else if (mod && e.key === "\\") { e.preventDefault(); app.view?.toggleInspector?.(); }
    else if (e.key === "/" && !isTyping(e) && !mod) { e.preventDefault(); app.view?.focusComposer?.(); }
    else if (e.key.toLowerCase() === "a" && !mod && !e.altKey && !e.shiftKey && !isTyping(e) && !$(".dialog-backdrop, .popover")) { e.preventDefault(); focusApproval(); }
    else if (e.key === "Escape" && $("#app").classList.contains("nav-open")) closeNav();
    else if (e.key === "Escape" && app.view?.closeInspectorOverlay?.()) e.preventDefault();
  });
  document.addEventListener("click", async (e) => {
    if (e.target.closest('[data-action="open-nav"]')) { openNav(); return; }
    const copy = e.target.closest("[data-copy-code]");   // Markdown 代码块的复制按钮（流式重绘时不用逐个绑定）
    if (copy) {
      const code = copy.closest(".code")?.querySelector("pre")?.textContent ?? "";
      if (await copyText(code)) flashCopied(copy); else toast("浏览器拒绝了剪贴板访问", { tone: "warning" });
    }
  });
}

// ====================================================================== 启动
async function boot() {
  renderSidebarStatic();
  installTooltips();
  installShortcuts();
  try {
    app.info = await api.get("/api/info");
  } catch (e) {
    $("#main").innerHTML = `<div class="empty-state" style="height:100%"><span class="ic">${icon("alert", "lg")}</span><h3>无法连接到 flywhl 服务</h3><p>${esc(e.message)}</p><button class="btn" type="button" data-action="reload">${icon("refresh")}重试</button></div>`;
    $('#main [data-action="reload"]').onclick = () => location.reload();   // CSP 不执行内联 onclick
    return;
  }
  for (const t of app.info.tools) app.toolFx[t.name] = t.effect;
  renderFoot();
  await Promise.all([loadSessions(), loadApprovals()]);
  if (!location.hash) {     // 首次打开：回到上次的会话
    const last = store.get("flywhl-last-session");
    if (last && app.sessions.some((s) => s.id === last)) history.replaceState(null, "", "#/s/" + last);
  }
  addEventListener("hashchange", () => route());
  route();
  setInterval(() => {
    if (!document.hidden) { loadSessions(); loadApprovals(); }
    else if (app.prefs.notify) loadApprovals();   // 页面在后台：只查审批，有新的才发桌面通知
  }, 6000);
  matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => renderFoot());
}

boot().catch((e) => { console.error(e); toast(e.message, { tone: "danger", title: "初始化失败" }); });
