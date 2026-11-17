"""本机直连读取（``fetch_backend = "direct"``）：不经第三方，目标网站看到的是本机出口 IP。

出口核验全部在 ``WebClient``：只连公网地址、钉 IP 建连、重定向最多 3 跳且每跳重新核验、解压后字节上限、类型白名单。
范围判定（``fetch_scope``）由工具传入，每一跳都要过：跨主机的重定向在 cited / ask 下会被拦住。
"""

from __future__ import annotations

from typing import Callable

from ..client import WebClient, WebError
from ..extract import to_text
from ..search import FetchBackend, Page

ACCEPT = ("text/html", "text/plain", "text/markdown", "text/*", "application/json", "application/xhtml+xml",
          "application/xml")
REDIRECTS = 3


class DirectFetch(FetchBackend):
    name = "direct"
    price = 0.0

    async def fetch(self, url: str, http: WebClient, *, max_bytes: int, in_scope: Callable[[str, str], bool]) -> Page:
        resp = await http.request("GET", url, in_scope=in_scope, redirects=REDIRECTS, max_bytes=max_bytes, truncate=True,
                                  accept=ACCEPT, headers={"Accept": "text/html,text/plain;q=0.9,*/*;q=0.1"})
        if resp.status in (401, 403, 451):
            raise WebError("blocked", f"站点拒绝访问（HTTP {resp.status}）", status=resp.status)
        if resp.status == 404 or resp.status == 410:
            raise WebError("no_content", f"页面不存在（HTTP {resp.status}）", status=resp.status)
        if resp.status == 429 or resp.status >= 500:
            raise WebError("transient", f"站点暂时不可用（HTTP {resp.status}）", status=resp.status)
        if resp.status >= 300:
            raise WebError("no_content", f"站点返回 HTTP {resp.status}", status=resp.status)
        title, text = to_text(resp.text(), resp.content_type)
        if not text:
            raise WebError("no_content", "页面没有可读的正文（可能要执行脚本才能显示）")
        return Page(url=url, title=title, content=text, status=resp.status, bytes=len(resp.body), truncated=resp.truncated,
                    final_url=resp.url)


__all__ = ["DirectFetch"]
