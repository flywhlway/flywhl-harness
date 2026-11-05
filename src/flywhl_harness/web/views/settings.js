// 「环境与安全」：当前进程实际生效的配置（只读）、安全体检、本机偏好。
// 配置只读是刻意的：修改入口是 flywhl.toml 与启动参数，高危设置要经过 --trust 确认；
// 页面若能改配置，拿到控制台的人就能绕过信任机制给 Agent 放权。
import { api } from "../lib/api.js";
import { $, esc, fmt, h, store } from "../lib/dom.js";
import { icon } from "../lib/icons.js";
import { modeOf } from "../lib/meta.js";
import { toast } from "../lib/ui.js";
import { pageShell } from "./pages.js";

const ON_ASK = { wait: "等待人工审批", deny: "自动拒绝", allow: "自动放行" };
const TONE = { ok: { cls: "success", icon: "checkCircle", label: "正常" }, warn: { cls: "warning", icon: "alert", label: "需留意" },
  bad: { cls: "danger", icon: "xCircle", label: "高风险" } };

/** 安全体检：每一项都给出“现在是什么”和“这意味着什么”，不只是一个红绿灯 */
function posture(s) {
  const P = s.permissions, sb = s.sandbox, ac = s.access, mode = modeOf(P.mode);
  const openHooks = s.hooks.filter((x) => x.event === "pre_tool_use" && !x.fail_closed).length;
  const loose = [...s.hooks, ...s.mcp].flatMap((x) => x.unpinned || []);
  const bound = new Set([...s.hooks, ...s.mcp].flatMap((x) => x.files || [])).size;
  return [
    { tone: ac.token ? "ok" : "warn", icon: "lock", label: "访问令牌", value: ac.token ? "已启用" : "已关闭",
      why: ac.token ? "页面与 API 都要求令牌；令牌保存在 ~/.flywhl/server_token（仅属主可读）" : "以 --no-token 启动：本机任何程序都能在这里批准命令、切换权限" },
    { tone: "ok", icon: "globe", label: "Host 校验", value: ac.hosts.join(" · "), why: "其他主机名一律拒绝，挡住 DNS 重绑定" },
    { tone: P.mode === "bypass" ? "bad" : "ok", icon: mode.icon, label: "默认权限模式", value: mode.label, why: mode.desc },
    { tone: P.on_ask === "allow" ? "bad" : "ok", icon: "user", label: "无人值守时",
      value: P.on_ask === "deny" && P.auto_approve.length ? "按规则自动批准" : ON_ASK[P.on_ask] || P.on_ask,
      why: P.on_ask === "allow" ? "需要审批的操作会直接执行（--yes），只应在一次性沙箱里使用"
        : P.on_ask === "deny" && P.auto_approve.length ? `${P.auto_approve.length} 条规则命中的操作自动批准，其余拒绝；高危命令、读过不可信内容后的外部操作照样拒绝`
        : `无人处理的审批 ${fmt.dur(P.approval_timeout)} 后按拒绝处理` },
    { tone: "ok", icon: "shieldCheck", label: "托管基线", value: `${P.baseline} 条规则`,
      why: "读写凭证位置、灾难性命令在任何模式下都拒绝，完全放行也不例外" },
    sb.problem
      ? { tone: "bad", icon: "box", label: "命令沙箱", value: "不可用，子进程会被拒绝", why: `${sb.problem}。修好之前 bash、hook、MCP 服务器都不会运行；确实要无沙箱运行，在用户配置里写 sandbox = "off"` }
      : sb.level === "off" && sb.network !== "allow"
      ? { tone: "bad", icon: "box", label: "命令沙箱", value: "未启用，bash 会被拒绝",
          why: `配置了网络策略 ${sb.network}，它要靠系统沙箱执行；没有沙箱时 bash 一律拒绝，不悄悄联网。请同时设置 permissions.sandbox` }
      : { tone: sb.level === "off" ? "warn" : "ok", icon: "box", label: "命令沙箱", value: sb.level === "off" ? "未启用" : `${LEVEL[sb.level] || sb.level} · ${NET[sb.network]}`,
          why: sb.level === "off" ? "已明确关闭：bash 只受命令分析与审批约束，没有内核级隔离" : "凭证位置与钥匙串不可读，写入只限工作区与临时目录，本机套接字只放行列出的" },
    { tone: P.strip_secret_env ? "ok" : "warn", icon: "eye", label: "密钥剥离", value: P.strip_secret_env ? "已启用" : "已关闭",
      why: P.strip_secret_env ? "bash 子进程看不到名字像密钥的环境变量，模型执行 env 也读不到" : "bash 子进程继承全部环境变量，包括 API Key" },
    { tone: s.snapshots ? "ok" : "warn", icon: "history", label: "写前快照", value: s.snapshots ? "已启用" : "已关闭",
      why: s.snapshots ? "写操作前用影子 git 打快照，可在检查器「历史」里回滚" : "改错的文件只能靠你自己的版本管理恢复" },
    { tone: openHooks ? "warn" : "ok", icon: "hook", label: "Hooks", value: s.hooks.length ? `${s.hooks.length} 个` : "未配置",
      why: openHooks ? `${openHooks} 个 pre_tool_use hook 出错时放行（fail_closed = false）` : s.hooks.length ? "pre_tool_use hook 出错或超时即拒绝执行（fail-closed）" : "可在 flywhl.toml 里用命令 hook 拦截或改写工具调用" },
    { tone: loose.length ? "warn" : "ok", icon: "layers", label: "供应链",
      value: loose.length ? `${loose.length} 个包没锁版本` : P.require_pinned ? "强制锁定版本" : bound ? `绑定 ${bound} 个文件` : "无临时拉取的包",
      why: loose.length ? `每次启动都可能拉到新发布的版本，包被接管就会投毒：${loose.join("；")}。锁到确切版本，或设置 permissions.require_pinned = true 拒绝启动`
        : `hook 与 MCP 引用的脚本按内容绑定：项目配置的信任哈希包含它们，命令 hook 每次执行前核对，改过就拒绝执行${P.require_pinned ? "；没锁版本的包拒绝启动" : ""}` },
  ];
}

const NET = { allow: "网络不限", deny: "禁止联网", allowlist: "出站白名单" };
const LEVEL = { macos: "macOS Seatbelt", linux: "Linux bubblewrap" };
/** 沙箱网络一栏：白名单要把放行了哪些主机写出来，空白名单等于禁止联网 */
function netLabel(sb) {
  if (sb.problem) return none("沙箱不可用");
  if (sb.level === "off") return sb.network === "allow" ? none("不限（沙箱未启用）") : `${esc(NET[sb.network])}<div class="muted">未生效：沙箱未启用，bash 命令会被拒绝</div>`;
  if (sb.network === "deny") return "禁止对外连接（含 DNS 查询与本机套接字）";
  if (sb.network === "allowlist") return sb.allow.length ? `只经出站代理连接：${sb.allow.map(code).join(" ")}` : "白名单为空，等同禁止联网";
  return "不限外网；本机 Unix 套接字除 DNS 外只放行列出的";
}

const kv = (rows) => `<dl class="kv wide">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;
const card = (iconName, title, body, aside = "") => `<section class="card"><div class="card-head">${icon(iconName)}<h2>${esc(title)}</h2>${aside ? `<span class="aside">${aside}</span>` : ""}</div><div class="card-body">${body}</div></section>`;
const none = (text) => `<span class="muted">${esc(text)}</span>`;
const code = (text) => `<code class="mono">${esc(text)}</code>`;
/** hook / MCP 实际的沙箱状态：单条可以和全局不同 */
const entryBox = ({ sandbox: b }) => !b ? ""
  : b.problem ? `<div class="warn-line">${icon("alert", "sm")}系统沙箱不可用，不会运行</div>`
  : b.level === "off" ? `<div class="warn-line">${icon("alert", "sm")}在系统沙箱外运行，能读凭证、能写工作区之外</div>`
  : `<div class="muted">在系统沙箱里运行 · ${esc(NET[b.network] || b.network)}${b.allow.length ? `：${b.allow.map(code).join(" ")}` : ""}</div>`;

/** hook / MCP 的供应链：按内容绑定的文件，没锁版本的包 */
const supply = (x) => `${(x.files || []).length ? `<div class="muted">按内容绑定：${x.files.map(code).join(" ")}</div>` : ""}${(x.unpinned || []).length ? `<div class="warn-line">${icon("alert", "sm")}没锁版本：${x.unpinned.map(esc).join("；")}</div>` : ""}`;

function render(inner, s, app) {
  const checks = posture(s);
  const count = (t) => checks.filter((c) => c.tone === t).length;
  const P = s.permissions, B = s.budget;
  inner.innerHTML = `
    <div class="page-intro"><p>当前进程实际生效的配置。这里只读：修改请编辑 <code>flywhl.toml</code> 或启动参数，放宽权限、hook、MCP 这类高危设置需要 <code>--trust</code> 确认后才会生效。</p></div>
    <section class="card posture-card" aria-labelledby="posture-t">
      <div class="card-head">${icon("shieldCheck")}<h2 id="posture-t">安全体检</h2>
        <span class="aside"><span class="badge success">${count("ok")} 项正常</span>${count("warn") ? `<span class="badge warning">${count("warn")} 项需留意</span>` : ""}${count("bad") ? `<span class="badge danger">${count("bad")} 项高风险</span>` : ""}</span></div>
      <ul class="posture">${checks.map((c) => `<li class="${c.tone}"><span class="st ${TONE[c.tone].cls}" role="img" aria-label="${TONE[c.tone].label}">${icon(TONE[c.tone].icon)}</span>
        <span class="lb">${icon(c.icon, "sm")}${esc(c.label)}</span><span class="v">${esc(c.value)}</span><span class="why">${esc(c.why)}</span></li>`).join("")}</ul>
    </section>
    <div class="settings-grid">
      ${card("cpu", "模型", kv([["供应商", esc(s.model.provider)], ["模型", code(s.model.model)],
        ["降级链", s.model.fallback.length ? s.model.fallback.map(code).join(" → ") : none("未配置")],
        ["上下文窗口", `<span class="tnum">${fmt.int(s.model.context_window)}</span> tokens`], ["单次输出上限", `<span class="tnum">${fmt.int(s.model.max_output)}</span> tokens`]]))}
      ${card("shield", "权限与审批", kv([["默认模式", esc(modeOf(P.mode).label)],
        ["无人值守时", esc(P.on_ask === "deny" && P.auto_approve.length ? "按规则自动批准，其余拒绝" : ON_ASK[P.on_ask] || P.on_ask)],
        ["审批超时", esc(fmt.dur(P.approval_timeout))], ["托管基线", `${esc(P.baseline)} 条（不可关闭）`],
        ["自定义规则", P.rules.length ? `<ul class="rule-list">${P.rules.map((r) => `<li>${code(r)}</li>`).join("")}</ul>` : none("无，使用模式默认值")],
        ["无人值守自动批准", P.auto_approve.length ? `<ul class="rule-list">${P.auto_approve.map((r) => `<li>${code(r)}</li>`).join("")}</ul>`
          + (P.on_ask === "deny" ? "" : none("on_ask = deny 时生效")) : none("无")]]),
        `${esc(s.tools)} 个工具`)}
      ${card("terminal", "命令执行", kv([["系统沙箱", s.sandbox.problem ? none("不可用") : s.sandbox.level === "off" ? none("未启用") : esc(s.sandbox.level)],
        ["沙箱网络", netLabel(s.sandbox)],
        ...(s.sandbox.level === "off" ? [] : [["放行的套接字", s.sandbox.unix_sockets.length ? s.sandbox.unix_sockets.map(code).join(" ") : none("无")]]),
        ["密钥剥离", P.strip_secret_env ? "已启用" : "已关闭"], ["写前快照", s.snapshots ? "已启用" : "已关闭"],
        ["完成验收", s.verify_command ? code(s.verify_command) : none("未配置")]]))}
      ${card("lock", "访问控制", kv([["访问令牌", s.access.token ? "已启用" : "已关闭"],
        ["可信 Host", s.access.hosts.map(code).join(" ")], ["工作区", code(s.workspace)]]))}
      ${card("hook", "Hooks", s.hooks.length ? `<ul class="plain-list">${s.hooks.map((x) => `<li><div class="row1">${code(x.event)}<span class="muted">匹配</span>${code(x.matcher)}
        <span class="badge ${x.fail_closed ? "success" : "warning"} square">${x.fail_closed ? "出错即拒绝" : "出错放行"}</span></div><div class="cmd">${esc(x.command)}</div>
        <div class="muted">${(x.env || []).length ? `密钥类变量已剥离，另外传入：${x.env.map(code).join(" ")}` : "密钥类环境变量已剥离"}</div>${entryBox(x)}${supply(x)}</li>`).join("")}</ul>`
        : `<p class="muted">没有配置命令 hook。</p>`, s.hooks.length ? `${s.hooks.length} 个` : "")}
      ${card("plug", "MCP 服务器", s.mcp.length ? kv(s.mcp.map((m) => [m.name, `${esc(m.transport)} · ${esc(m.tools)} 个工具
        <div class="muted">${m.trust_annotations ? "采信服务器自报的注解" : `不采信服务器自报的注解${m.ignored_hints ? `（${esc(m.ignored_hints)} 个工具自称只读、幂等或封闭世界，已忽略）` : ""}`}${(m.read_only || []).length ? `；按只读处理：${m.read_only.map(code).join(" ")}` : ""}</div>${entryBox(m)}${supply(m)}`])) : `<p class="muted">没有连接 MCP 服务器。</p>`)}
      ${card("gauge", "单次运行预算", kv([["最多轮次", fmt.int(B.max_turns)], ["最多工具调用", fmt.int(B.max_tool_calls)],
        ["token 上限", B.max_total_tokens ? fmt.int(B.max_total_tokens) : none("不限")], ["费用上限", B.max_cost_usd ? esc("$" + B.max_cost_usd) : none("不限")],
        ["墙钟上限", B.max_wall_seconds ? esc(fmt.dur(B.max_wall_seconds)) : none("不限")]]))}
      <section class="card" id="prefs"></section>
    </div>`;
  renderPrefs($("#prefs", inner), app);
}

/** 本机偏好：只存在这个浏览器里（localStorage），不影响服务端 */
function renderPrefs(box, app) {
  const supported = "Notification" in window;
  const perm = supported ? Notification.permission : "unsupported";
  const on = !!app.prefs.notify && perm === "granted";
  const history = store.get("flywhl-history", []);
  const state = !supported ? "当前浏览器或访问方式不支持（需要 localhost 或 HTTPS）"
    : perm === "denied" ? "浏览器已拒绝通知权限：请在地址栏左侧的站点设置里允许后再开启"
    : on ? "页面在后台时，有新的审批请求、运行结束都会提醒你" : "关闭";
  box.innerHTML = `<div class="card-head">${icon("monitor")}<h2>本机偏好</h2><span class="aside">只保存在这个浏览器里</span></div>
    <div class="card-body pref-list">
      <div class="pref"><div><div class="pref-t">${icon("bell", "sm")}桌面通知</div><div class="pref-d">${esc(state)}</div></div>
        <button class="switch" type="button" role="switch" aria-checked="${on}" aria-label="桌面通知" ${!supported || perm === "denied" ? "disabled" : ""}><span></span></button></div>
      <div class="pref"><div><div class="pref-t">${icon("history", "sm")}输入历史</div><div class="pref-d">在空输入框里按 ↑ 翻阅发送过的任务；当前保存了 ${history.length} 条（最多 50 条）</div></div>
        <button class="btn sm" type="button" data-act="clear-history" ${history.length ? "" : "disabled"}>清空</button></div>
    </div>`;
  $(".switch", box).onclick = async () => {
    if (on) { app.prefs.notify = false; renderPrefs(box, app); return; }
    const res = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
    app.prefs.notify = res === "granted";
    if (res === "granted") toast("页面在后台时，有新的审批请求、运行结束都会提醒你。", { title: "已开启桌面通知", tone: "success" });
    renderPrefs(box, app);
  };
  $('[data-act="clear-history"]', box).onclick = () => { store.set("flywhl-history", []); toast("输入历史已清空。", { tone: "success" }); renderPrefs(box, app); };
}

export function mountSettings(root, app) {
  const { inner, actions } = pageShell(root, { title: "环境与安全", iconName: "shield" });
  inner.innerHTML = `<div class="empty-state"><span class="spinner" style="width:20px;height:20px"></span><p>正在读取配置…</p></div>`;
  const load = async (manual = false) => {
    try { render(inner, await api.get("/api/settings"), app); if (manual) toast("已刷新。", { tone: "success" }); }
    catch (e) { inner.innerHTML = ""; inner.append(h("div", { class: "empty-state", html: `<span class="ic">${icon("alert", "lg")}</span><h3>无法读取配置</h3><p>${esc(e.message)}</p>` })); }
  };
  actions.append(h("button", { class: "btn", type: "button", onclick: () => load(true), html: `${icon("refresh")}<span class="lbl-sm-hide">刷新</span>` }));
  load();
  return { dispose() {} };
}
