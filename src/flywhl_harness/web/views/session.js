// 会话工作区：页头（状态、权限模式、运行控制）+ 时间线 + 输入区 + 检查器。
// 数据流：初次渲染来自会话时间线（持久化状态）；运行中的增量来自 SSE（带 seq，按 seq 去重，可断线续传）；
// Run 结束时再以持久化状态重绘一次，保证页面与数据库一致。
import { api } from "../lib/api.js";
import { $, $$, copyText, esc, fmt, h, isTyping, modKey } from "../lib/dom.js";
import { icon } from "../lib/icons.js";
import { ACTIVE, DEMO_TEMPLATES, EVENT_TYPES, MODES, REAL_TEMPLATES, RUN_STATUS, TERMINAL, modeOf, statusBadge } from "../lib/meta.js";
import { announce, confirmDialog, openMenu, toast } from "../lib/ui.js";
import { createInspector } from "./inspector.js";
import { md } from "../md.js";
import * as T from "./timeline.js";

export function mountSession(root, app, sid, { pendingPrompt = null } = {}) {
  const V = {
    sid, detail: null, run: null, running: false, es: null, lastSeq: 0, events: [], trail: [], trailFor: null,
    usage: null, live: null, inject: "steer", conn: "idle", showDeltas: false, evFilter: "all", evQuery: "", disposed: false,
  };
  const ctx = {
    get sid() { return V.sid; },
    toolFx: app.toolFx,
    openSession: (id) => { app.navigate("#/s/" + id); toast("这是子代理的隔离会话：它看不到父会话的历史，只收到了任务简报。", { title: "子代理会话" }); },
    onResolved: (a) => { removeApproval(a.call_id); app.refreshApprovals(); },
    onSubApproval: (d, card) => attachApproval(d, card),
  };

  // ------------------------------------------------------------------ 布局
  root.innerHTML = "";
  const header = h("header", { class: "page-header" });
  header.innerHTML = `
    <button class="btn ghost icon menu-toggle" type="button" data-action="open-nav" aria-label="打开导航">${icon("menu")}</button>
    <div class="title"><h1 id="s-title">新任务</h1><span id="s-status"></span><span class="meta" id="s-meta"></span></div>
    <div class="actions">
      <span class="live-ind hide-sm" id="s-live" data-state="idle" aria-live="polite"><span class="dot"></span><span class="t">未连接</span></span>
      <span class="divider-v hide-sm"></span>
      <button class="btn" type="button" id="s-mode" aria-haspopup="menu" aria-expanded="false"></button>
      <span class="actions" id="s-run-actions"></span>
      <button class="btn ghost icon" type="button" id="s-insp" aria-label="检查器" data-tip="显示或隐藏检查器（${modKey} + \\）">${icon("panel")}</button>
    </div>`;
  const ws = h("div", { class: "workspace" });
  const conv = h("section", { class: "conversation", "aria-label": "对话" });
  const timeline = h("div", { class: "timeline", id: "timeline", tabindex: "-1" });
  const inner = h("div", { class: "timeline-inner", role: "log", "aria-live": "off", "aria-label": "对话记录" });
  timeline.append(inner);
  const jump = h("button", { class: "btn sm jump-latest", type: "button", hidden: true, html: `${icon("arrowDown", "sm")}跳到最新` });
  const composerWrap = buildComposer();
  conv.append(timeline, jump, composerWrap);
  const insp = createInspector(V, {
    get info() { return app.info; },
    resumeRun: (r) => resumeRun(r),
  });
  ws.append(conv, insp.el);
  root.append(header, ws);

  const setInspector = (open, remember = true) => {
    if (remember) app.prefs.inspector = open;      // 只记住用户的主动选择；按屏幕宽度推断的默认值不写入偏好
    ws.classList.toggle("no-inspector", !open);
    $("#s-insp", header).setAttribute("aria-pressed", String(open));
    if (open) insp.render();
  };
  setInspector(innerWidth >= 1280 ? app.prefs.inspector ?? true : false, false);
  $("#s-insp", header).onclick = () => setInspector(ws.classList.contains("no-inspector"));
  $("#s-mode", header).onclick = (e) => openModeMenu(e.currentTarget);
  jump.onclick = () => { stick = true; timeline.scrollTo({ top: timeline.scrollHeight, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" }); jump.hidden = true; };

  // 贴底滚动：用户在底部附近时跟随新内容；往上翻阅时不打断，只显示“跳到最新”
  let stick = true;
  timeline.addEventListener("scroll", () => {
    stick = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 80;
    if (stick) jump.hidden = true;
  }, { passive: true });
  const follow = () => { if (stick) timeline.scrollTop = timeline.scrollHeight; else jump.hidden = false; };
  const add = (node) => { inner.append(node); follow(); return node; };

  // ------------------------------------------------------------------ 输入区
  function buildComposer() {
    const w = h("div", { class: "composer-wrap" });
    w.innerHTML = `
      <div class="composer-status" id="c-status"></div>
      <form class="composer" id="c-form">
        <label class="sr-only" for="c-input">任务描述</label>
        <textarea id="c-input" rows="1" placeholder="描述你要完成的任务…" enterkeyhint="send"></textarea>
        <div class="composer-bar">
          <span id="c-left"></span>
          <span class="grow"></span>
          <span class="hint hide-sm"><span class="kbd">Enter</span>发送<span class="kbd" style="margin-left:6px">Shift</span>+<span class="kbd">Enter</span>换行</span>
          <button class="btn primary" type="submit" id="c-send" disabled>${icon("arrowUp")}<span id="c-send-t">发送</span></button>
        </div>
      </form>`;
    const ta = $("#c-input", w), send = $("#c-send", w);
    const autosize = () => { ta.style.height = "auto"; ta.style.height = Math.min(240, ta.scrollHeight) + "px"; send.disabled = !ta.value.trim(); };
    ta.addEventListener("input", autosize);
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
    });
    $("#c-form", w).addEventListener("submit", (e) => { e.preventDefault(); submit(); });
    w.autosize = autosize;
    return w;
  }
  const input = $("#c-input", composerWrap);

  function renderComposerLeft() {
    const left = $("#c-left", composerWrap);
    left.innerHTML = "";
    if (V.running) {
      const seg = h("div", { class: "segmented", role: "radiogroup", "aria-label": "运行中输入的处理方式" });
      for (const [id, label, ic, tip] of [["steer", "插话", "steer", "在下一个安全点注入，不打断正在执行的工具"], ["follow_up", "排队", "queue", "当前任务结束后再处理"]]) {
        const b = h("button", { type: "button", role: "radio", "aria-checked": String(V.inject === id), "data-tip": tip, html: `${icon(ic, "sm")}${label}` });
        b.onclick = () => { V.inject = id; renderComposerLeft(); updateSendLabel(); input.focus(); };
        seg.append(b);
      }
      left.append(seg);
    } else {
      const b = h("button", { class: "btn ghost sm", type: "button", "aria-haspopup": "menu", "aria-expanded": "false", html: `${icon("layers", "sm")}模板` });
      const list = app.info.provider === "mock" ? DEMO_TEMPLATES : REAL_TEMPLATES;
      b.onclick = () => openMenu(b, list.map((t) => ({ label: t.title, desc: t.desc, icon: t.icon, onSelect: () => { input.value = t.prompt; composerWrap.autosize(); input.focus(); } })), { align: "start", label: "任务模板" });
      left.append(b);
    }
  }
  function updateSendLabel() {
    $("#c-send-t", composerWrap).textContent = V.running ? (V.inject === "steer" ? "插话" : "排队") : "发送";
    input.placeholder = V.running ? (V.inject === "steer" ? "补充或纠正方向，将在下一个安全点注入…" : "当前任务结束后要做的事…") : "描述你要完成的任务…";
  }
  function renderComposerStatus() {
    const box = $("#c-status", composerWrap);
    const st = V.run?.status;
    const u = V.usage || {};
    let html = "";
    if (st === "waiting_human") html = `<span class="badge warning">${icon("shieldAlert")}等待审批</span><span>请在上方确认或拒绝待执行的操作</span>`;
    else if (st === "cancelling") html = `<span class="spinner" style="width:12px;height:12px;border-width:1.5px"></span><span>正在停止：进行中的不可中断操作会先完成</span>`;
    else if (V.running) html = `<span class="spinner" style="width:12px;height:12px;border-width:1.5px;color:var(--brand)"></span><span>运行中${u.turns ? ` · 第 ${u.turns} 轮` : ""}${u.usage ? ` · ${fmt.k(fmt.tokTotal(u.usage))} tokens` : ""}</span>`;
    else if (st === "paused") html = `<span class="badge">${icon("pauseCircle")}已暂停</span><span>可以继续运行，期间工作区的变化会告知模型</span>`;
    else if (st === "interrupted") html = `<span class="badge warning">${icon("alert")}已中断</span><span>进程曾意外退出，可以从中断处继续</span>`;
    else if (st === "failed") html = `<span class="badge danger">${icon("xCircle")}运行失败</span><span>可以重试：从已保存的历史继续，不会重复已成功的步骤</span>`;
    box.innerHTML = html;
  }

  // ------------------------------------------------------------------ 页头
  function renderHeader() {
    const s = V.detail?.session;
    const title = s?.title || "新任务";
    $("#s-title", header).textContent = title;
    document.title = `${title} · flywhl harness`;
    $("#s-status", header).innerHTML = V.run ? statusBadge(V.run.status) : "";
    $("#s-meta", header).innerHTML = V.run ? `<span class="mono">${esc(fmt.shortId(V.run.id))}</span>${V.run.attempt > 1 ? `<span>· 第 ${V.run.attempt} 次尝试</span>` : ""}` : "";
    const mode = modeOf(s?.meta?.mode || app.info.mode);
    const mb = $("#s-mode", header);
    mb.className = "btn" + (mode.danger ? " danger-outline" : "");
    mb.innerHTML = `${icon(mode.icon)}<span class="lbl-sm-hide">${esc(mode.label)}</span>${icon("chevronDown", "sm")}`;
    mb.setAttribute("aria-label", `权限模式：${mode.label}`);
    mb.dataset.tip = mode.desc;
    renderRunActions();
  }

  function renderRunActions() {
    const box = $("#s-run-actions", header);
    box.innerHTML = "";
    const st = V.run?.status;
    if (V.running) {
      if (st !== "cancelling") {
        box.append(h("button", { class: "btn", type: "button", "data-tip": "在下一个安全点暂停，进行中的工具会先执行完", onclick: pauseRun, html: `${icon("pause")}<span class="lbl-sm-hide">暂停</span>` }));
        box.append(h("button", { class: "btn danger-outline", type: "button", "data-tip": "协作式取消：可中断的操作立即停止，不可中断的会先完成", onclick: stopRun, html: `${icon("stop")}<span class="lbl-sm-hide">停止</span>` }));
      } else box.append(h("button", { class: "btn", type: "button", disabled: true, html: `<span class="spinner" style="width:12px;height:12px;border-width:1.5px"></span>正在停止` }));
    } else if (st === "paused" || st === "interrupted") {
      box.append(h("button", { class: "btn primary", type: "button", onclick: () => resumeRun(), html: `${icon("play")}继续运行` }));
    } else if (st === "failed") {
      box.append(h("button", { class: "btn primary", type: "button", onclick: () => resumeRun(), html: `${icon("retry")}重试` }));
    }
  }

  function openModeMenu(anchor) {
    if (!V.sid) { toast("权限模式属于会话：发送第一条任务后可以调整。", { title: "尚未创建会话" }); return; }
    const cur = V.detail?.session?.meta?.mode || app.info.mode;
    openMenu(anchor, [{ head: "权限模式（下一次运行生效）" }, ...MODES.map((m) => ({
      label: m.label, desc: m.desc, icon: m.icon, checked: m.id === cur, danger: m.danger,
      onSelect: () => setMode(m),
    }))], { label: "权限模式" });
  }

  async function setMode(m) {
    if (m.danger) {
      const ok = await confirmDialog({ title: "切换到完全放行？", desc: "所有操作（写文件、执行命令、外部调用）都将直接执行，不再请求确认。仅应在隔离的沙箱环境中使用。灾难性命令仍会被拒绝。", confirmText: "仍然切换", danger: true });
      if (!ok) return;
    }
    try {
      await api.post(`/api/sessions/${V.sid}/mode`, { mode: m.id });
      V.detail.session.meta.mode = m.id;
      renderHeader();
      toast(`${m.label}：${m.desc}`, { tone: m.danger ? "warning" : "success", title: "权限模式已更新（下一次运行生效）" });
    } catch (e) { toast(e.message, { tone: "danger" }); }
  }

  // ------------------------------------------------------------------ 运行控制
  async function submit() {
    const text = input.value.trim();
    if (!text) return;
    try {
      if (!V.sid) {        // 草稿：第一条任务才真正创建会话，标题取自任务描述
        const s = await api.post("/api/sessions", { title: text.slice(0, 40) });
        input.value = "";
        app.navigate("#/s/" + s.id, { pendingPrompt: text });
        return;
      }
      if (V.running && V.run) {
        await api.post(`/api/runs/${V.run.id}/steer`, { text, mode: V.inject });
        toast(V.inject === "steer" ? "将在下一个安全点注入，不会打断正在执行的工具。" : "当前任务结束后会接着处理。", { title: V.inject === "steer" ? "插话已排队" : "已加入队列", tone: "success" });
      } else await startRun(text);
      input.value = "";
      composerWrap.autosize();
    } catch (e) { toast(e.message, { tone: "danger", title: "发送失败" }); }
  }

  async function startRun(text) {
    const run = await api.post(`/api/sessions/${V.sid}/runs`, { prompt: text });
    V.run = run; V.trail = []; V.trailFor = run.id; V.usage = null;
    V.detail?.runs.unshift(run);
    $(".start", inner)?.remove();
    setRunning(true);
    connect(run.id, 0);
    app.refreshSessions();
  }

  async function pauseRun() {
    try { await api.post(`/api/runs/${V.run.id}/pause`); toast("将在下一个安全点暂停，进行中的工具会先执行完。", { title: "已请求暂停" }); }
    catch (e) { toast(e.message, { tone: "danger" }); }
  }
  async function stopRun() {
    try { await api.post(`/api/runs/${V.run.id}/cancel`); toast("可中断的操作会立即停止，不可中断的操作会先完成。", { title: "正在停止" }); }
    catch (e) { toast(e.message, { tone: "danger" }); }
  }
  async function resumeRun(r = V.run) {
    if (!r) return;
    try {
      const nr = await api.post(`/api/runs/${r.id}/${r.status === "failed" ? "retry" : "resume"}`);
      V.run = nr; V.trail = []; V.trailFor = nr.id;
      setRunning(true);
      connect(nr.id, nr.last_seq);
      toast(r.status === "failed" ? "从已保存的历史继续，不会重复已成功的步骤。" : "已从暂停处继续。", { title: r.status === "failed" ? "开始重试" : "继续运行", tone: "success" });
    } catch (e) { toast(e.message, { tone: "danger", title: "无法继续运行" }); }
  }

  function setRunning(on) {
    V.running = on;
    renderHeader();
    renderComposerLeft();
    updateSendLabel();
    renderComposerStatus();
  }

  // ------------------------------------------------------------------ SSE：订阅、续传
  function setConn(state) {
    V.conn = state;
    const el = $("#s-live", header);
    el.dataset.state = state;
    $(".t", el).textContent = { live: "实时", reconnecting: "重连中", idle: V.running ? "未连接" : "空闲" }[state] || "空闲";
  }
  function connect(runId, after) {
    disconnect();
    V.lastSeq = after || 0;
    const es = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events?after=${V.lastSeq}`);
    V.es = es;
    for (const t of EVENT_TYPES) es.addEventListener(t, (e) => onEvent(JSON.parse(e.data)));
    es.onopen = () => setConn("live");
    es.onerror = () => { if (V.es === es && es.readyState !== EventSource.CLOSED) setConn("reconnecting"); };
    es.addEventListener("stream_end", () => { es.close(); if (V.es === es) V.es = null; setConn("idle"); });
  }
  function disconnect() { if (V.es) { V.es.close(); V.es = null; } setConn("idle"); }
  V.simulateDrop = () => {
    if (!V.es || !V.run) return;
    const seq = V.lastSeq, rid = V.run.id;
    disconnect();
    gap(`连接已断开（最后序号 ${seq}），Run 仍在服务端继续执行`);
    setTimeout(() => { if (V.disposed) return; gap(`重连：Last-Event-ID = ${seq}，服务端补发之后的事件`); connect(rid, seq); }, 2000);
  };
  const gap = (text) => { const ev = { type: "__gap", text, seq: V.lastSeq }; V.events.push(ev); insp.onEvent(ev); };

  function onEvent(ev) {
    if (V.disposed || ev.seq <= V.lastSeq) return;   // 按 seq 去重：续传时不会重复处理
    V.lastSeq = ev.seq;
    V.events.push(ev);
    if (V.events.length > 2000) V.events.splice(0, 400);
    try { (H[ev.type] || (() => {}))(ev.data || {}, ev); } catch (err) { console.error("处理事件失败", ev, err); }
    insp.onEvent(ev);
  }

  const H = {
    run_start() { setRunning(true); },
    status(d) {
      V.trail.push(d);
      if (V.run) V.run.status = d.to;
      if (TERMINAL.has(d.to) || d.to === "paused") setRunning(false);
      else { renderHeader(); renderComposerStatus(); }
      if (d.to === "waiting_human") announce("有操作等待你的审批");
    },
    message_start(d) { if (d.role === "assistant") startLive(d.message_id); },
    message_update(d) {
      const L = V.live;
      if (!L) return;
      if (d.kind === "text") { L.text += d.delta; scheduleLive(); }
      else if (d.kind === "thinking") { L.thinking += d.delta; scheduleLive(); }
      else if (d.kind === "toolcall_start") {
        const card = T.toolCard({ id: d.id || `idx${d.index}`, name: d.name || "…", arguments: {} }, null, ctx, { streaming: true });
        card.dataset.index = d.index;
        L.tools.append(card);
        follow();
      } else if (d.kind === "toolcall") {
        const card = $$(".tool", L.tools).find((c) => c.dataset.index == d.index);
        if (card) $(".args", card).textContent += d.delta;
      }
    },
    message_end(d) {
      const m = d.message;
      if (m.role === "assistant") finalizeLive(m);
      else if (m.role === "user") add(T.userItem(m));
    },
    model_retry(d) {
      if (d.discard_partial && V.live) {
        V.live.text = ""; V.live.thinking = ""; V.live.tools.innerHTML = "";
        V.live.thinkingEl?.remove(); V.live.thinkingEl = null;
        scheduleLive();
      }
      add(T.noticeItem({ level: "warn", text: `模型调用失败（${d.kind}），${d.delay}s 后第 ${d.attempt} 次重试${d.discard_partial ? "；已丢弃不完整的输出" : ""}` }));
    },
    tool_start(d) { T.markRunning(ensureCard({ id: d.call_id, name: d.name, arguments: d.args }), d); follow(); },
    tool_update(d) {
      const card = $("#tc-" + CSS.escape(d.call_id));
      if (!card) return;
      const u = d.update || {};
      if (u.output) T.appendLiveOutput(card, u.output);
      if (u.progress) T.setToolState(card, `<span class="spinner" style="width:12px;height:12px;border-width:1.5px"></span>${esc(u.progress)}`, "run");
      if (u.subagent) T.subagentLine(card, u.subagent, ctx);
    },
    tool_end(d) { T.setToolResult(ensureCard({ id: d.call_id, name: d.name, arguments: {} }), d.message, ctx); follow(); },
    approval_request(d) { attachApproval(d); app.refreshApprovals(); },
    approval_resolved(d) { removeApproval(d.call_id); app.refreshApprovals(); },
    compaction_start(d) { add(T.pendingCompaction(d)); },
    compaction_end(d) {
      $$(".pending-comp", inner).forEach((x) => x.remove());
      if (d.skipped) { add(T.noticeItem({ level: "info", text: d.reason })); return; }
      add(T.compactionDivider({ tokens_before: d.tokens_before, tokens_after: d.tokens_after, summary: d.summary, summarized_messages: d.summarized }));
    },
    notice(d) { add(T.noticeItem(d)); },
    usage(d) { V.usage = d; renderComposerStatus(); },
    error(d) { add(T.noticeItem({ level: "error", text: "运行时错误：" + d.error })); },
    async run_end(d) {
      setRunning(false);
      const m = RUN_STATUS[d.status];
      announce(`运行结束：${m ? m.label : d.status}`);
      await reload();
      V.run = V.detail.runs.find((r) => r.id === V.run?.id) || V.run;
      setRunning(false);
      app.refreshSessions();
      insp.render();
    },
  };

  // ------------------------------------------------------------------ 流式渲染
  function startLive(id) {
    $(".start", inner)?.remove();
    const w = h("article", { class: "msg-assistant", id: "m-" + id, "aria-busy": "true" });
    const body = h("div", { class: "md streaming" });
    const tools = h("div", { class: "tools" });
    w.append(body, tools);
    add(w);
    V.live = { id, el: w, text: "", thinking: "", body, tools, thinkingEl: null, raf: 0 };
  }
  function scheduleLive() {
    const L = V.live;
    if (!L || L.raf) return;
    L.raf = requestAnimationFrame(() => {          // 用 rAF 合并高频增量：每帧最多重绘一次
      L.raf = 0;
      if (L.thinking) {
        if (!L.thinkingEl) { L.thinkingEl = T.thinkingBlock("", { open: true, live: true }); L.el.prepend(L.thinkingEl); }
        const pre = $("pre", L.thinkingEl);
        pre.textContent = L.thinking;
        pre.scrollTop = pre.scrollHeight;
        $(".aside", L.thinkingEl).textContent = `${fmt.int(L.thinking.length)} 字`;
      }
      L.body.innerHTML = md(L.text);   // md() 先转义再渲染，模型输出里的 HTML 不会生效
      follow();
    });
  }
  function finalizeLive(m) {
    const fresh = T.assistantItem(m, {}, ctx);
    if (V.live && V.live.id === m.id) { cancelAnimationFrame(V.live.raf); V.live.el.replaceWith(fresh); }
    else add(fresh);
    V.live = null;
    follow();
  }
  function ensureCard(call) {
    let card = $("#tc-" + CSS.escape(call.id));
    if (!card) { card = T.toolCard(call, null, ctx); add(card); }
    return card;
  }

  // ------------------------------------------------------------------ 审批
  function attachApproval(a, hostCard) {
    if ($("#ap-" + CSS.escape(a.id))) return;
    const card = T.approvalCard(a, ctx);
    const tool = $("#tc-" + CSS.escape(a.call_id));
    if (tool) T.setToolState(tool, `${icon("shieldAlert", "sm")}等待审批`, "warn");
    const host = hostCard || tool;
    if (host) host.after(card); else add(card);
    follow();
  }
  function removeApproval(callId) {
    $$(`.approval[data-call="${CSS.escape(callId)}"]`, inner).forEach((x) => x.remove());
    const tool = $("#tc-" + CSS.escape(callId));
    if (tool?.classList.contains("running")) T.setToolState(tool, `<span class="spinner" style="width:12px;height:12px;border-width:1.5px"></span>执行中`, "run");
  }

  // ------------------------------------------------------------------ 时间线（持久化状态）
  function renderTimeline() {
    inner.innerHTML = "";
    const d = V.detail;
    if (!d || !d.timeline.length) {
      inner.append(T.startPage({
        info: app.info, mode: modeOf(d?.session?.meta?.mode || app.info.mode),
        templates: app.info.provider === "mock" ? DEMO_TEMPLATES : REAL_TEMPLATES,
        onPick: (p) => { input.value = p; composerWrap.autosize(); submit(); },
      }));
      return;
    }
    const results = {};
    for (const it of d.timeline) if (it.kind === "message" && it.message.role === "tool") results[it.message.tool_call_id] = it.message;
    const runs = Object.fromEntries(d.runs.map((r) => [r.id, r]));
    let prev = null;
    const frag = document.createDocumentFragment();
    for (const it of d.timeline) {
      if (prev && it.run_id && it.run_id !== prev && runs[prev] && TERMINAL.has(runs[prev].status)) frag.append(T.runSummary(runs[prev]));
      prev = it.run_id || prev;
      if (it.kind === "compaction") { frag.append(T.compactionDivider(it.payload)); continue; }
      const m = it.message;
      if (m.role === "user") frag.append(T.userItem(m));
      else if (m.role === "assistant") frag.append(T.assistantItem(m, results, ctx));
    }
    if (prev && runs[prev] && TERMINAL.has(runs[prev].status)) frag.append(T.runSummary(runs[prev]));
    inner.append(frag);
    for (const a of d.approvals) if (a.session_id === V.sid || V.running) attachApproval(a);
    stick = true;
    timeline.scrollTop = timeline.scrollHeight;
    requestAnimationFrame(() => (timeline.scrollTop = timeline.scrollHeight));   // 等布局稳定（代码块、表格）后再贴底一次
  }

  async function reload() {
    if (!V.sid) return;
    V.detail = await api.get(`/api/sessions/${V.sid}`);
    renderTimeline();
    renderHeader();
  }
  V.reload = reload;

  // ------------------------------------------------------------------ 启动
  async function init() {
    if (!V.sid) {
      V.detail = null;
      renderTimeline(); renderHeader(); setRunning(false);
      insp.render();
      input.focus();
      return;
    }
    try { await reload(); }
    catch (e) {
      inner.innerHTML = "";
      inner.append(h("div", { class: "empty-state", html: `<span class="ic">${icon("alert", "lg")}</span><h3>无法打开这个会话</h3><p>${esc(e.message)}</p>` }));
      const back = h("a", { class: "btn", href: "#/" }, "返回开始页");
      inner.firstElementChild.append(back);
      return;
    }
    const ar = V.detail.active_run;
    V.run = ar || V.detail.runs[0] || null;
    setRunning(!!ar);
    if (ar) connect(ar.id, ar.last_seq);
    insp.render();
    if (pendingPrompt) await startRun(pendingPrompt).catch((e) => toast(e.message, { tone: "danger", title: "启动运行失败" }));
    if (!isTyping({ target: document.activeElement })) input.focus({ preventScroll: true });
  }
  init();

  return {
    dispose() { V.disposed = true; disconnect(); },
    focusComposer() { input.focus(); },
    toggleInspector() { setInspector(ws.classList.contains("no-inspector")); },
    setMode(id) { const m = MODES.find((x) => x.id === id); if (m) setMode(m); },
    stop() { if (V.running) stopRun(); },
    get running() { return V.running; },
    get sid() { return V.sid; },
  };
}
