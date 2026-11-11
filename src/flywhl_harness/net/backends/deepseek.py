"""DeepSeek Search / Fetch（``POST /v1/tools/search``、``/v1/tools/fetch``）。

与模型共用 ``DEEPSEEK_API_KEY`` 和账户余额。要点（2026-11 官方文档）：

- ``query`` 1–128 字符，时间和站点过滤写在查询里：``after:YYYY-MM-DD``、``site:域名``。拼上运算符之后再校验长度，
  超出就让模型缩短，不静默截断；
- ``need_content`` 固定 false：正文统一走 ``web_fetch``；``ori_question`` 只在 ``send_question = true`` 时发；
- 响应的 ``summary``（600–800 字）给模型，``snippet`` 给界面；``rerank_score`` 0–3 用于排序和过滤，
  ``authority`` ≥ 3 在给模型的行里标“权威”；``site_icon`` 丢弃（前端不加载第三方图标）；
- 只有 200 且结果非空才计费；限流 Search 100 QPS、Fetch 25 QPS；403 / 404 只出现在 Fetch。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from ..client import WebClient, WebError
from ..search import FRESHNESS_DAYS, FetchBackend, Page, SearchBackend, SearchHit, SearchQuery, check

DEFAULT_BASE = "https://api.deepseek.com"
QUERY_LIMIT = 128
PRICE = 0.00075   # 美元 / 次成功调用，Search 与 Fetch 同价（2026-11 官方价目）；[web.backend.deepseek] price_per_call 可覆盖


class _DeepSeek:
    service = "DeepSeek"

    def __init__(self, opts: Mapping[str, Any], env: Mapping[str, str]):
        self.key_env = str(opts.get("api_key_env") or "DEEPSEEK_API_KEY")
        self._key = env.get(self.key_env) or ""
        price = opts.get("price_per_call")
        self.price = PRICE if price is None else float(price)
        base = str(opts.get("endpoint") or DEFAULT_BASE).rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        self.base = base
        self.hosts = ((urlsplit(base).hostname or "").lower(),)

    def missing(self) -> str | None:
        return None if self._key else f"缺少密钥（环境变量 {self.key_env} 为空）"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}


class DeepSeekSearch(_DeepSeek, SearchBackend):
    name = "deepseek"

    def __init__(self, opts: Mapping[str, Any], env: Mapping[str, str], *, today: Callable[[], _dt.date] = _dt.date.today):
        super().__init__(opts, env)
        self.send_question = bool(opts.get("send_question", False))
        self.today = today
        self.question: str | None = None   # send_question 时由工具填入本次任务原文

    def compose(self, q: SearchQuery) -> str:
        parts = [q.q.strip()]
        if q.site:
            parts.append(f"site:{q.site.strip()}")
        if q.freshness in FRESHNESS_DAYS:
            parts.append(f"after:{(self.today() - _dt.timedelta(days=FRESHNESS_DAYS[q.freshness])).isoformat()}")
        text = " ".join(parts)
        if len(text) > QUERY_LIMIT:
            raise WebError("bad_request", f"查询词加上站点 / 时间过滤后有 {len(text)} 个字符，超过 DeepSeek 的上限 {QUERY_LIMIT}，"
                                          "请缩短关键词")
        return text

    async def search(self, q: SearchQuery, http: WebClient, *, question: str | None = None) -> list[SearchHit]:
        body: dict[str, Any] = {"query": self.compose(q), "count": q.count, "need_content": False}
        if self.send_question and question:
            body["ori_question"] = question[:QUERY_LIMIT]
        data = check(await self.post(http, f"{self.base}/v1/tools/search", body, self._headers()), self.service)
        out: list[SearchHit] = []
        for r in (data or {}).get("results") or []:
            if not isinstance(r, dict) or not r.get("url"):
                continue
            auth = r.get("authority")
            out.append(SearchHit(
                title=str(r.get("title") or ""), url=str(r["url"]), snippet=str(r.get("snippet") or ""),
                site=str(r.get("site_name") or ""), published=_date(r.get("date")),
                content=str(r.get("summary") or "") or None,
                score=float(r["rerank_score"]) if isinstance(r.get("rerank_score"), (int, float)) else None,
                authority=int(auth) if isinstance(auth, (int, float)) else None))
        return out


class DeepSeekFetch(_DeepSeek, FetchBackend):
    name = "deepseek"

    async def fetch(self, url: str, http: WebClient, *, max_bytes: int, in_scope: Callable[[str, str], bool]) -> Page:
        if len(url) > 8192:
            raise WebError("bad_request", "网址超过 8192 个字符")
        resp = await http.request("POST", f"{self.base}/v1/tools/fetch", json={"url": url}, headers=self._headers(),
                                  in_scope=lambda host, _u: host in self.hosts, max_bytes=max_bytes + 65536,
                                  accept=("application/json",))
        data = check(resp, self.service, fetch=True) or {}
        content = str(data.get("content") or "")
        if not content.strip():
            raise WebError("no_content", "取不到可用内容（站点拒绝、类型不支持或没有正文）")
        return Page(url=url, title=str(data.get("title") or ""), content=content, status=200,
                    bytes=len(content.encode("utf-8")), final_url=url)


def _date(v: Any) -> str | None:
    """RFC 3339 → ISO 日期；解析不了就是 None，不猜。"""
    if not v or not isinstance(v, str):
        return None
    try:
        return _dt.datetime.fromisoformat(v.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return v[:10] if len(v) >= 10 and v[4] == "-" and v[7] == "-" else None


__all__ = ["DeepSeekFetch", "DeepSeekSearch"]
