"""博查 Web Search（``POST https://api.bochaai.com/v1/web-search``），境内后端，做 DeepSeek 的替补。

请求 ``{query, freshness, summary: true, count}``；``freshness`` 取 ``oneDay / oneWeek / oneMonth / oneYear / noLimit``。
结果在 ``data.webPages.value[]``：``name``、``url``、``snippet``、``summary``、``siteName``、``datePublished``。
错误既可能是 HTTP 状态码，也可能是 200 里带 ``code``：两种都按状态码表归类，只有 403 例外——博查用它表示余额不足
（调研摘录，未在官方文档上逐条核实），按 ``quota`` 处理：换下一家并熔断，与鉴权失败一样都会换后端。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..client import WebClient, WebError
from ..search import SearchBackend, SearchHit, SearchQuery, check, classify_status, error_detail

ENDPOINT = "https://api.bochaai.com/v1/web-search"
_FRESHNESS = {"day": "oneDay", "week": "oneWeek", "month": "oneMonth", "year": "oneYear"}


class BochaSearch(SearchBackend):
    name = "bocha"
    hosts = ("api.bochaai.com",)

    def __init__(self, opts: Mapping[str, Any], env: Mapping[str, str]):
        self.key_env = str(opts.get("api_key_env") or "BOCHA_API_KEY")
        self._key = env.get(self.key_env) or ""
        self.price = float(opts.get("price_per_call") or 0.0)

    def missing(self) -> str | None:
        return None if self._key else f"缺少密钥（环境变量 {self.key_env} 为空）"

    async def search(self, q: SearchQuery, http: WebClient, *, question: str | None = None) -> list[SearchHit]:
        text = f"{q.q.strip()} site:{q.site.strip()}" if q.site else q.q.strip()
        body = {"query": text, "freshness": _FRESHNESS.get(q.freshness or "", "noLimit"), "summary": True, "count": q.count}
        resp = await self.post(http, ENDPOINT, body, {"Authorization": f"Bearer {self._key}"})
        if resp.status == 403:
            raise _quota(error_detail(resp))
        data = check(resp, "博查") or {}
        code = data.get("code")
        if isinstance(code, int) and code != 200:
            detail = str(data.get("msg") or data.get("message") or "")
            raise _quota(detail) if code == 403 else classify_status(code, "博查", detail=detail)
        pages = ((data.get("data") or {}).get("webPages") or {}).get("value") or []
        return [SearchHit(title=str(r.get("name") or ""), url=str(r["url"]), snippet=str(r.get("snippet") or ""),
                          site=str(r.get("siteName") or ""), published=_day(r.get("datePublished")),
                          content=str(r.get("summary") or "") or None)
                for r in pages if isinstance(r, dict) and r.get("url")]


def _quota(detail: str) -> WebError:
    tail = f"：{detail[:200]}" if detail else ""
    return WebError("quota", f"博查余额不足或无权调用（HTTP 403）{tail}", status=403)


def _day(v: Any) -> str | None:
    return v[:10] if isinstance(v, str) and len(v) >= 10 and v[4] == "-" else None


__all__ = ["BochaSearch"]
