"""自建 SearXNG（``GET <endpoint>?q=...&format=json``）。

实例要在 ``settings.yml`` 的 ``search.formats`` 里打开 ``json``，否则返回 403。没有密钥、不计费；只有摘要，没有长正文。
部署在内网时要显式写 ``allow_private = true``：只对这一个后端主机放行内网和回环地址（云元数据、链路本地照样拒绝），
也只有这时才接受 ``http://`` 端点——公网上明文发查询词等于把它交给沿途每一跳。
"""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit

from ..client import WebClient, WebError
from ..search import SearchBackend, SearchHit, SearchQuery, check

_RANGE = {"day": "day", "week": "week", "month": "month", "year": "year"}


class SearxngSearch(SearchBackend):
    name = "searxng"

    def __init__(self, opts: Mapping[str, Any]):
        self.endpoint = str(opts.get("endpoint") or "").strip()
        self.price = float(opts.get("price_per_call") or 0.0)
        host = (urlsplit(self.endpoint).hostname or "").lower() if self.endpoint else ""
        self.hosts = (host,) if host else ()
        private = bool(opts.get("allow_private", False))
        self.allow_private = self.hosts if private else ()
        self.allow_http = self.hosts if private else ()

    def missing(self) -> str | None:
        if not self.endpoint:
            return "没有配置 endpoint"
        if not self.hosts:
            return "endpoint 不是合法网址"
        return None

    async def search(self, q: SearchQuery, http: WebClient, *, question: str | None = None) -> list[SearchHit]:
        params = {"q": f"{q.q.strip()} site:{q.site.strip()}" if q.site else q.q.strip(), "format": "json"}
        if q.freshness in _RANGE:
            params["time_range"] = _RANGE[q.freshness]   # type: ignore[index]
        sep = "&" if "?" in self.endpoint else "?"
        resp = await http.request("GET", f"{self.endpoint}{sep}{urlencode(params)}", in_scope=self.in_scope,
                                  max_bytes=4_000_000, accept=("application/json",), allow_http=self.allow_http,
                                  allow_private=self.allow_private)
        if resp.status == 403:
            raise WebError("auth", "SearXNG 拒绝了 JSON 请求（HTTP 403）：实例要在 settings.yml 的 search.formats 里打开 json")
        data = check(resp, "SearXNG") or {}
        return [SearchHit(title=str(r.get("title") or ""), url=str(r["url"]), snippet=str(r.get("content") or ""), site="",
                          published=_day(r.get("publishedDate")),
                          score=float(r["score"]) if isinstance(r.get("score"), (int, float)) else None)
                for r in data.get("results") or [] if isinstance(r, dict) and r.get("url")]


def _day(v: Any) -> str | None:
    return v[:10] if isinstance(v, str) and len(v) >= 10 and v[4] == "-" else None


__all__ = ["SearxngSearch"]
