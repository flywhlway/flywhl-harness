"""``web_fetch`` 直连读取的正文抽取：HTML → Markdown 风格的纯文本。

只用标准库的 ``html.parser``：不执行脚本、不加载子资源、不解析 CSS。丢掉 script / style / 导航这类非正文元素，
标题转成 ``#``，列表项转成 ``-``，链接只留文字（URL 不进正文：模型要读链接得用搜索结果或用户给的地址，
这样“出现过的 URL”名单不会被网页内容撑大）。
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_SKIP = {"script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "embed", "head", "nav",
         "footer", "form", "button", "select", "textarea"}
_BLOCK = {"p", "div", "section", "article", "main", "header", "aside", "table", "tr", "ul", "ol", "dl", "dt", "dd",
          "blockquote", "pre", "figure", "figcaption", "hr", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6"}
_VOID = {"br", "hr", "img", "input", "meta", "link", "source", "wbr", "area", "base", "col", "embed", "param", "track"}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.pre = 0
        self.title: list[str] = []
        self.in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:   # type: ignore[override]
        if tag == "title":
            self.in_title = True
        if tag in _SKIP:
            if tag not in _VOID:
                self.skip += 1
            return
        if self.skip:
            return
        if tag == "pre":
            self.pre += 1
        if tag in _BLOCK:
            self.out.append("\n")
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("- ")
        elif tag in ("td", "th"):
            self.out.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag in _SKIP and tag not in _VOID:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "pre":
            self.pre = max(0, self.pre - 1)
        if tag in _BLOCK:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title.append(data)
        if self.skip:
            return
        self.out.append(data if self.pre else re.sub(r"\s+", " ", data))


def html_to_text(doc: str) -> tuple[str, str]:
    """返回 (标题, 正文)。"""
    p = _Text()
    try:
        p.feed(doc)
        p.close()
    except Exception:   # pragma: no cover - html.parser 很宽容，这里只是保险
        pass
    text = "".join(p.out)
    lines = [ln.strip() for ln in text.split("\n")]
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return " ".join("".join(p.title).split()), body


def to_text(body: str, content_type: str) -> tuple[str, str]:
    if content_type in ("text/html", "application/xhtml+xml"):
        return html_to_text(body)
    if content_type in ("application/xml", "text/xml"):
        return "", html.unescape(re.sub(r"<[^>]{0,500}>", " ", body)).strip()
    return "", body.strip()


__all__ = ["html_to_text", "to_text"]
