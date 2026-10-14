// DOM 与格式化小工具。约定：所有插入 innerHTML 的动态文本都先经过 esc()。
import { esc } from "../md.js";

export { esc };
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/** 创建元素：h("button", { class: "btn", onclick, "aria-label": "关闭" }, "文字", childNode) */
export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k === "text") node.textContent = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) if (c != null && c !== false) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
}

/** 解析一段 HTML 字符串为单个元素 */
export function frag(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

export const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
export const modKey = isMac ? "⌘" : "Ctrl";

export const fmt = {
  k(n) {
    n = Number(n) || 0;
    if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "K";
    return String(Math.round(n));
  },
  int: (n) => (Number(n) || 0).toLocaleString("zh-CN"),
  money(n) {
    n = Number(n) || 0;
    if (n === 0) return "$0";
    return n < 0.01 ? "<$0.01" : "$" + n.toFixed(n < 1 ? 3 : 2);
  },
  ago(ts) {
    if (!ts) return "—";
    const s = Date.now() / 1000 - ts;
    if (s < 60) return "刚刚";
    if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
    if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
    if (s < 86400 * 7) return `${Math.floor(s / 86400)} 天前`;
    return new Date(ts * 1000).toLocaleDateString("zh-CN");
  },
  time: (ts) => (ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "—"),
  dur(sec) {
    sec = Math.max(0, Number(sec) || 0);
    if (sec < 1) return `${Math.round(sec * 1000)}ms`;
    if (sec < 60) return `${sec.toFixed(sec < 10 ? 1 : 0)}s`;
    const m = Math.floor(sec / 60), s = Math.round(sec % 60);
    if (m < 60) return `${m}m ${s}s`;
    return `${Math.floor(m / 60)}h ${m % 60}m`;
  },
  shortId: (id) => (id ? String(id).slice(0, 12) : "—"),
};

export function debounce(fn, ms = 200) {
  let t = 0;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; }
  catch {
    const ta = h("textarea", { style: "position:fixed;opacity:0" }, text);
    document.body.append(ta); ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  }
}

/** 正在输入时（输入框、可编辑区域）不响应单键快捷键 */
export function isTyping(e) {
  const t = e.target;
  return t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
}

export const store = {
  get(k, d = null) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* 隐私模式等：忽略 */ } },
};
