// 交互基础组件：提示（toast）、屏幕阅读器播报、对话框、下拉菜单、悬浮说明、命令面板。
// 共同约定：浮层打开时焦点进入浮层、Esc 关闭、关闭后焦点回到触发它的元素。
import { $, $$, esc, h, modKey } from "./dom.js";
import { icon } from "./icons.js";

// ====================================================================== 播报 / 提示
let announceTimer = 0;
export function announce(text) {
  const el = $("#announcer");
  if (!el) return;
  el.textContent = "";
  clearTimeout(announceTimer);
  announceTimer = setTimeout(() => (el.textContent = text), 60);   // 先清空再写入：相同文本也会重新播报
}

const TOAST_ICONS = { success: "checkCircle", warning: "alert", danger: "xCircle", info: "info" };
export function toast(message, { tone = "info", title = "", timeout = 4200 } = {}) {
  const box = $("#toasts");
  while (box.children.length >= 4) box.firstElementChild.remove();
  const t = h("div", { class: `toast ${tone}`, html: `<span class="ic">${icon(TOAST_ICONS[tone] || "info")}</span>
    <div>${title ? `<div class="ttl">${esc(title)}</div>` : ""}<div class="msg">${esc(message)}</div></div>` });
  const close = h("button", { class: "btn ghost icon sm", "aria-label": "关闭提示", html: icon("x", "sm") });
  const dismiss = () => { t.classList.add("leaving"); setTimeout(() => t.remove(), 140); };
  close.onclick = dismiss;
  t.append(close);
  box.append(t);
  let timer = setTimeout(dismiss, timeout);
  t.onmouseenter = () => clearTimeout(timer);                       // 悬停时暂停自动消失
  t.onmouseleave = () => (timer = setTimeout(dismiss, 1800));
}

// ====================================================================== 焦点陷阱
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';
function trapFocus(root, e) {
  if (e.key !== "Tab") return;
  const items = $$(FOCUSABLE, root).filter((x) => x.offsetParent !== null);
  if (!items.length) return;
  const first = items[0], last = items[items.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

// ====================================================================== 对话框
/**
 * 通用对话框。返回 Promise：确认时 resolve(onConfirm 的返回值 ?? true)，取消时 resolve(null)。
 * onConfirm 抛错时对话框保持打开，并在底部显示错误（例如表单校验失败）。
 */
export function dialog({ title, desc = "", tone = "", iconName = "info", body = null, confirmText = "确定", cancelText = "取消",
  danger = false, wide = false, onConfirm = null, hideCancel = false }) {
  return new Promise((resolve) => {
    const prev = document.activeElement;
    const id = "dlg-" + Math.random().toString(36).slice(2, 8);
    const backdrop = h("div", { class: "dialog-backdrop" });
    const box = h("div", { class: `dialog${wide ? " wide" : ""}`, role: "dialog", "aria-modal": "true", "aria-labelledby": id + "-t", "aria-describedby": desc ? id + "-d" : null });
    box.innerHTML = `<div class="dialog-head"><span class="ic ${tone}">${icon(iconName, "lg")}</span>
      <div><h2 id="${id}-t">${esc(title)}</h2>${desc ? `<p class="desc" id="${id}-d">${esc(desc)}</p>` : ""}</div></div>`;
    const bodyEl = h("div", { class: "dialog-body" });
    if (body) bodyEl.append(body);
    const err = h("div", { class: "field-error", role: "alert", hidden: true });
    bodyEl.append(err);
    const foot = h("div", { class: "dialog-foot" });
    const cancel = h("button", { class: "btn", type: "button" }, cancelText);
    const ok = h("button", { class: `btn ${danger ? "danger" : "primary"}`, type: "button" }, confirmText);
    if (!hideCancel) foot.append(cancel);
    foot.append(ok);
    box.append(bodyEl, foot);
    backdrop.append(box);
    $("#overlay-root").append(backdrop);

    const close = (val) => {
      document.removeEventListener("keydown", onKey, true);
      backdrop.remove();
      if (prev && prev.focus) prev.focus();
      resolve(val);
    };
    const confirm = async () => {
      try {
        ok.disabled = true;
        const v = onConfirm ? await onConfirm(box) : true;
        close(v === undefined ? true : v);
      } catch (e) {
        ok.disabled = false;
        err.hidden = false;
        err.innerHTML = `${icon("alert", "sm")}<span>${esc(e.message || e)}</span>`;
      }
    };
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(null); }
      else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); confirm(); }
      else trapFocus(box, e);
    };
    document.addEventListener("keydown", onKey, true);
    cancel.onclick = () => close(null);
    ok.onclick = confirm;
    backdrop.addEventListener("mousedown", (e) => { if (e.target === backdrop) close(null); });
    const firstInput = $("textarea, input", box);
    (firstInput || (danger ? cancel : ok)).focus();   // 危险操作默认聚焦“取消”
  });
}

export const confirmDialog = (opts) => dialog({ iconName: opts.danger ? "alert" : "info", tone: opts.danger ? "danger" : "", ...opts });

export function promptDialog({ title, desc, label, placeholder = "", value = "", multiline = true, confirmText = "确定", danger = false, help = "" }) {
  const fid = "pf-" + Math.random().toString(36).slice(2, 8);
  const field = h("div", { class: "field" });
  field.innerHTML = `<label class="field-label" for="${fid}">${esc(label)}</label>`;
  const input = multiline ? h("textarea", { class: "textarea", id: fid, rows: 3, placeholder }) : h("input", { class: "input", id: fid, placeholder });
  input.value = value;
  field.append(input);
  if (help) field.append(h("div", { class: "field-help" }, help));
  return dialog({ title, desc, body: field, confirmText, danger, tone: danger ? "danger" : "", iconName: danger ? "alert" : "pencil", onConfirm: () => input.value.trim() });
}

// ====================================================================== 下拉菜单
let openMenuState = null;
export function closeMenu() {
  if (!openMenuState) return;
  const { pop, anchor, onKey, onDown } = openMenuState;
  document.removeEventListener("keydown", onKey, true);
  document.removeEventListener("mousedown", onDown, true);
  pop.remove();
  anchor.setAttribute("aria-expanded", "false");
  openMenuState = null;
}

/**
 * items: { label, desc?, icon?, checked?, danger?, onSelect } | "sep" | { head: "分组名" }
 * 键盘：↑↓ 移动、Enter 选择、Esc 关闭；关闭后焦点回到触发按钮。
 */
export function openMenu(anchor, items, { align = "end", label = "" } = {}) {
  if (openMenuState?.anchor === anchor) { closeMenu(); return; }
  closeMenu();
  const pop = h("div", { class: "popover", role: "menu", "aria-label": label || null });
  for (const it of items) {
    if (it === "sep") { pop.append(h("div", { class: "menu-sep", role: "separator" })); continue; }
    if (it.head) { pop.append(h("div", { class: "menu-head" }, it.head)); continue; }
    const b = h("button", { class: `menu-item${it.danger ? " danger" : ""}`, role: it.checked != null ? "menuitemradio" : "menuitem", "aria-checked": it.checked != null ? String(!!it.checked) : null, type: "button" });
    b.innerHTML = `<span>${it.icon ? icon(it.icon) : ""}</span><span class="lbl">${esc(it.label)}</span><span class="check-mark">${it.checked ? icon("check") : ""}</span>${it.desc ? `<span class="desc">${esc(it.desc)}</span>` : ""}`;
    b.onclick = () => { closeMenu(); anchor.focus(); it.onSelect?.(); };
    pop.append(b);
  }
  $("#overlay-root").append(pop);
  const r = anchor.getBoundingClientRect();
  const w = pop.offsetWidth, hgt = pop.offsetHeight;
  let left = align === "end" ? r.right - w : r.left;
  left = Math.max(8, Math.min(left, innerWidth - w - 8));
  let top = r.bottom + 6;
  if (top + hgt > innerHeight - 8) top = Math.max(8, r.top - hgt - 6);
  Object.assign(pop.style, { left: left + "px", top: top + "px" });
  anchor.setAttribute("aria-expanded", "true");
  const buttons = $$(".menu-item", pop);
  const current = buttons.findIndex((b) => b.getAttribute("aria-checked") === "true");
  (buttons[current >= 0 ? current : 0])?.focus();
  const onKey = (e) => {
    const i = buttons.indexOf(document.activeElement);
    if (e.key === "Escape") { e.preventDefault(); closeMenu(); anchor.focus(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); buttons[(i + 1) % buttons.length].focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); buttons[(i - 1 + buttons.length) % buttons.length].focus(); }
    else if (e.key === "Tab") closeMenu();
  };
  const onDown = (e) => { if (!pop.contains(e.target) && !anchor.contains(e.target)) closeMenu(); };
  document.addEventListener("keydown", onKey, true);
  document.addEventListener("mousedown", onDown, true);
  openMenuState = { pop, anchor, onKey, onDown };
}

// ====================================================================== 悬浮说明（data-tip）
// 鼠标悬停或键盘聚焦 400ms 后出现；触屏设备上不出现（说明文字同时放在 aria-label / 可见文本里）
let tipEl = null, tipTimer = 0;
function showTip(target) {
  const text = target.getAttribute("data-tip");
  if (!text) return;
  hideTip();
  tipEl = h("div", { class: "tooltip", role: "tooltip" }, text);
  document.body.append(tipEl);
  const r = target.getBoundingClientRect();
  const w = tipEl.offsetWidth, hgt = tipEl.offsetHeight;
  let top = r.bottom + 8;
  if (top + hgt > innerHeight - 8) top = r.top - hgt - 8;
  tipEl.style.left = Math.max(8, Math.min(r.left + r.width / 2 - w / 2, innerWidth - w - 8)) + "px";
  tipEl.style.top = top + "px";
}
function hideTip() { clearTimeout(tipTimer); tipEl?.remove(); tipEl = null; }
export function installTooltips() {
  if (matchMedia("(pointer: coarse)").matches) return;
  const enter = (e) => { const t = e.target.closest?.("[data-tip]"); if (t) { clearTimeout(tipTimer); tipTimer = setTimeout(() => showTip(t), 400); } };
  document.addEventListener("mouseover", enter);
  document.addEventListener("focusin", (e) => { if (e.target.matches?.(":focus-visible")) enter(e); });
  document.addEventListener("mouseout", (e) => { if (e.target.closest?.("[data-tip]")) hideTip(); });
  document.addEventListener("focusout", hideTip);
  document.addEventListener("mousedown", hideTip);
  addEventListener("scroll", hideTip, true);
}

// ====================================================================== 命令面板
/**
 * commands: [{ group, label, hint?, icon?, keywords?, run }]
 * 输入即过滤（标签 + 关键词，大小写不敏感），↑↓ 选择、Enter 执行、Esc 关闭。
 */
export function openPalette(getCommands) {
  if ($(".palette")) return;
  const prev = document.activeElement;
  const backdrop = h("div", { class: "dialog-backdrop" });
  const box = h("div", { class: "dialog palette", role: "dialog", "aria-modal": "true", "aria-label": "命令面板" });
  box.innerHTML = `<div class="palette-input">${icon("search", "lg")}
      <input type="text" role="combobox" aria-expanded="true" aria-controls="palette-list" aria-autocomplete="list" placeholder="搜索会话或输入命令…" autocomplete="off" spellcheck="false"></div>
    <div class="palette-list" id="palette-list" role="listbox" aria-label="命令"></div>
    <div class="palette-foot"><span><span class="kbd">↑</span><span class="kbd">↓</span>选择</span><span><span class="kbd">↵</span>执行</span><span><span class="kbd">Esc</span>关闭</span><span style="margin-left:auto"><span class="kbd">${modKey}</span><span class="kbd">K</span>随时打开</span></div>`;
  backdrop.append(box);
  $("#overlay-root").append(backdrop);
  const input = $("input", box), list = $(".palette-list", box);
  const all = getCommands();
  let shown = [], sel = 0;

  const render = () => {
    const q = input.value.trim().toLowerCase();
    shown = all.filter((c) => !q || (c.label + " " + (c.keywords || "") + " " + (c.group || "")).toLowerCase().includes(q)).slice(0, 60);
    sel = Math.min(sel, Math.max(0, shown.length - 1));
    list.innerHTML = "";
    if (!shown.length) { list.append(h("div", { class: "palette-empty" }, "没有匹配的命令或会话")); input.removeAttribute("aria-activedescendant"); return; }
    let group = null;
    shown.forEach((c, i) => {
      if (c.group !== group) { group = c.group; list.append(h("div", { class: "palette-group", role: "presentation" }, group)); }
      const it = h("div", { class: "palette-item", role: "option", id: "pc-" + i, "aria-selected": String(i === sel) });
      it.innerHTML = `${icon(c.icon || "chevronRight")}<span class="lbl">${esc(c.label)}</span>${c.hint ? `<span class="hint">${esc(c.hint)}</span>` : ""}`;
      it.onmousemove = () => { if (sel !== i) { sel = i; update(); } };
      it.onclick = () => run(i);
      list.append(it);
    });
    update();
  };
  const update = () => {
    $$(".palette-item", list).forEach((x, i) => x.setAttribute("aria-selected", String(i === sel)));
    const cur = $("#pc-" + sel, list);
    if (cur) { input.setAttribute("aria-activedescendant", cur.id); cur.scrollIntoView({ block: "nearest" }); }
  };
  const close = (refocus = true) => { document.removeEventListener("keydown", onKey, true); backdrop.remove(); if (refocus && prev?.focus) prev.focus(); };
  const run = (i) => { const c = shown[i]; if (!c) return; close(false); c.run(); };
  const onKey = (e) => {
    if (e.key === "Escape") { e.preventDefault(); close(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); sel = (sel + 1) % Math.max(1, shown.length); update(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); sel = (sel - 1 + shown.length) % Math.max(1, shown.length); update(); }
    else if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); run(sel); }
    else if (e.key === "Tab") e.preventDefault();
  };
  document.addEventListener("keydown", onKey, true);
  input.oninput = () => { sel = 0; render(); };
  backdrop.addEventListener("mousedown", (e) => { if (e.target === backdrop) close(); });
  render();
  input.focus();
}
