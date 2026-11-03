// 极简 Markdown 渲染：代码块、行内代码、标题、列表、表格、引用、粗斜体、链接。
// 先转义再渲染，杜绝模型输出里的 HTML 注入。
import { icon } from "./lib/icons.js";

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function inline(s) {
  const codes = [];
  s = s.replace(/`([^`]+)`/g, (_, c) => `\u0000${codes.push(c) - 1}\u0000`);
  s = esc(s)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${esc(codes[+i])}</code>`);
}

// ====================================================================== 代码高亮
// 只认注释、字符串、数字、关键字四类记号：够看清结构，不追求语法精确。每个记号单独转义后再包 <span>，
// 没被识别的片段同样转义——高亮不会成为 HTML 注入的入口
const FAMILY = {
  py: "py", python: "py", sh: "sh", bash: "sh", zsh: "sh", shell: "sh", console: "sh", js: "js", javascript: "js",
  mjs: "js", ts: "js", typescript: "js", jsx: "js", tsx: "js", json: "json", go: "go", rs: "rs", rust: "rs",
  java: "c", kotlin: "c", c: "c", cpp: "c", "c++": "c", cs: "c", swift: "c", toml: "conf", yaml: "conf", yml: "conf",
  ini: "conf", sql: "sql", diff: "diff",
};
const KEYWORDS = {
  py: "def class return if elif else for while in not and or is None True False import from as with try except finally raise lambda yield async await pass break continue global nonlocal assert del match case",
  sh: "if then else elif fi for while until do done case esac function in return export local readonly unset source exit set",
  js: "const let var function return if else for while do in of new class extends import from export default async await try catch finally throw typeof instanceof null undefined true false this switch case break continue yield interface type enum implements",
  json: "true false null",
  go: "func package import return if else for range go defer chan map struct interface type var const nil true false switch case break continue select",
  rs: "fn let mut pub struct enum impl trait use mod return if else for while loop match in as ref self Self true false None Some Ok Err async await move const static where crate",
  c: "int long short char float double void bool auto const static struct enum union return if else for while do switch case break continue new delete class public private protected virtual override final true false null nullptr this import package fun val var func let",
  conf: "true false null yes no on off",
  sql: "select from where insert into values update set delete create table index join left right inner outer on group by order having limit as and or not null is in distinct union all with",
};
const COMMENT = { py: "(?<![\\w$])#[^\\n]*", sh: "(?<![\\w$])#[^\\n]*", conf: "(?<![\\w$])#[^\\n]*", sql: "--[^\\n]*",
  js: "\\/\\/[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/", go: "\\/\\/[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/", rs: "\\/\\/[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/",
  c: "\\/\\/[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/" };
const STRING = `"""[\\s\\S]*?"""|'''[\\s\\S]*?'''|"(?:\\\\.|[^"\\\\\\n])*"|'(?:\\\\.|[^'\\\\\\n])*'|\`(?:\\\\.|[^\`\\\\])*\``;
const NUMBER = "\\b(?:0x[\\da-fA-F]+|\\d[\\d_]*(?:\\.\\d+)?(?:[eE][+-]?\\d+)?)\\b";
const PATTERNS = {};

function pattern(fam) {
  if (!PATTERNS[fam]) {
    const parts = [];
    if (COMMENT[fam]) parts.push(`(?<c>${COMMENT[fam]})`);
    parts.push(`(?<s>${STRING})`, `(?<n>${NUMBER})`, `(?<k>\\b(?:${KEYWORDS[fam].split(" ").join("|")})\\b)`);
    PATTERNS[fam] = new RegExp(parts.join("|"), fam === "sql" ? "gi" : "g");
  }
  return PATTERNS[fam];
}

export function highlight(code, lang) {
  const fam = FAMILY[String(lang || "").toLowerCase()];
  if (fam === "diff") {
    return code.split("\n").map((l) => {
      const c = l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : l.startsWith("@@") ? "k" : "";
      return c ? `<span class="hl-${c}">${esc(l)}</span>` : esc(l);
    }).join("\n");
  }
  if (!fam || code.length > 40000) return esc(code);   // 超长代码不高亮：每帧都会重新渲染，正则成本要可控
  let out = "", last = 0;
  for (const m of code.matchAll(pattern(fam))) {
    const g = m.groups;
    out += esc(code.slice(last, m.index)) + `<span class="hl-${g.c ? "c" : g.s ? "s" : g.n ? "n" : "k"}">${esc(m[0])}</span>`;
    last = m.index + m[0].length;
  }
  return out + esc(code.slice(last));
}

/** 代码块：语言标签 + 复制按钮（点击由 app.js 统一委托处理，流式重绘时不用重新绑定） */
function codeBlock(code, lang) {
  return `<div class="code"><div class="code-head"><span class="code-lang">${esc(lang || "text")}</span>`
    + `<button class="code-copy" type="button" data-copy-code aria-label="复制代码">${icon("copy", "sm")}<span>复制</span></button></div>`
    + `<pre><code class="lang-${esc(lang)}">${highlight(code, lang)}</code></pre></div>`;
}

export function md(src) {
  const lines = String(src ?? "").replace(/\r/g, "").split("\n");
  const out = [];
  let i = 0;
  const para = [];
  const flush = () => { if (para.length) { out.push(`<p>${inline(para.join("\n")).replace(/\n/g, "<br>")}</p>`); para.length = 0; } };
  while (i < lines.length) {
    const ln = lines[i];
    const fence = ln.match(/^\s*(`{3,})\s*([\w+#.-]*)/);
    if (fence) {
      flush();
      const buf = [];
      i++;
      // 闭合围栏至少和开头一样长、后面不带语言名（CommonMark）：代码里举例的 ```python 不会提前结束代码块
      const close = new RegExp(`^\\s*${fence[1]}\`*\\s*$`);
      while (i < lines.length && !close.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push(codeBlock(buf.join("\n"), fence[2]));
      continue;
    }
    const hd = ln.match(/^(#{1,4})\s+(.*)$/);
    if (hd) { flush(); out.push(`<h${hd[1].length}>${inline(hd[2])}</h${hd[1].length}>`); i++; continue; }
    if (/^\s*\|.*\|\s*$/.test(ln) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      flush();
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const head = cells(ln);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(cells(lines[i++]));
      out.push(`<table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${rows
        .map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
      continue;
    }
    const li = ln.match(/^\s*([-*]|\d+\.)\s+(.*)$/);
    if (li) {
      flush();
      const ordered = /\d/.test(li[1]);
      const items = [];
      while (i < lines.length) {
        const m = lines[i].match(/^\s*([-*]|\d+\.)\s+(.*)$/);
        if (!m) break;
        items.push(`<li>${inline(m[2])}</li>`);
        i++;
      }
      out.push(`<${ordered ? "ol" : "ul"}>${items.join("")}</${ordered ? "ol" : "ul"}>`);
      continue;
    }
    if (ln.startsWith(">")) { flush(); out.push(`<blockquote>${inline(ln.replace(/^>\s?/, ""))}</blockquote>`); i++; continue; }
    if (/^\s*(---|\*\*\*)\s*$/.test(ln)) { flush(); out.push("<hr>"); i++; continue; }
    if (!ln.trim()) { flush(); i++; continue; }
    para.push(ln);
    i++;
  }
  flush();
  return out.join("");
}
