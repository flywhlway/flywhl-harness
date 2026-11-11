"""搜索与读取的后端适配层：归一化、故障转移、熔断、并发上限、会话内缓存。

每家后端只做三件事：构造请求、映射响应、把失败归到下表的一类。重试、换后端、计费口径都在这里统一处理。

=============  ==============================  ===================================================
分类           典型来源                        处理
=============  ==============================  ===================================================
auth           401、403（搜索）、缺密钥        换下一家；全部失败时提示检查密钥变量
quota          402 余额不足                    换下一家，并在本进程内熔断该后端 10 分钟
rate_limited   429                             按 Retry-After（无则指数退避）重试，耗尽后换下一家
transient      408、5xx、超时、连接失败        同上
bad_request    400、413、查询超长              不重试，原样告诉模型（多半是查询词或运算符写错）
blocked        读取服务拒绝、出口核验拒绝      不重试
no_content     读取服务取不到正文（404）       不重试
=============  ==============================  ===================================================

所有后端都失败且最后一类是 rate_limited / transient 时抛 ``TransientToolError``，执行器在剩余时间内整体再试。
200 但没有结果不算失败，也不计费（DeepSeek 的计费口径）。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..config import WebConfig
from ..tools.base import TransientToolError
from .client import Fetched, WebClient, WebError

BREAKER_SECONDS = 600.0
CONCURRENCY = 4
_RETRIES = 2
_MAX_WAIT = 5.0
_CACHE_SIZE = 256
FRESHNESS_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}


@dataclass
class SearchHit:
    title: str
    url: str                       # 规范化后：小写主机、去掉 utm_* 与片段
    snippet: str                   # 给界面
    site: str                      # 站点名，缺省为主机名
    published: str | None = None   # ISO 日期；后端给不出就是 None，不猜
    content: str | None = None     # 后端给的长摘要：给模型
    score: float | None = None     # 后端的质量分（DeepSeek rerank_score、SearXNG score）
    authority: int | None = None   # DeepSeek 的权威度 0–4，-1 未知


@dataclass(frozen=True)
class SearchQuery:
    q: str
    count: int
    freshness: str | None = None   # day | week | month | year
    site: str | None = None


@dataclass
class Page:
    """读取到的网页。``content`` 是给模型的纯文本 / Markdown。"""

    url: str
    title: str
    content: str
    status: int = 200
    bytes: int = 0
    truncated: bool = False
    final_url: str | None = None


class SearchBackend:
    name = ""
    price = 0.0                    # 每次成功调用的美元单价
    hosts: tuple[str, ...] = ()    # 只允许连这些主机
    allow_private: tuple[str, ...] = ()
    allow_http: tuple[str, ...] = ()

    def missing(self) -> str | None:
        """不可用的原因（缺密钥、缺端点），可用时返回 None。"""
        return None

    async def search(self, q: SearchQuery, http: WebClient, *, question: str | None = None) -> list[SearchHit]:
        raise NotImplementedError   # pragma: no cover - 协议

    def in_scope(self, host: str, url: str) -> bool:
        return host in self.hosts

    async def post(self, http: WebClient, url: str, body: dict[str, Any], headers: dict[str, str] | None = None,
                   *, max_bytes: int = 4_000_000) -> Fetched:
        return await http.request("POST", url, json=body, headers=headers, in_scope=self.in_scope, max_bytes=max_bytes,
                                  accept=("application/json",), allow_http=self.allow_http, allow_private=self.allow_private)


class FetchBackend:
    name = ""
    price = 0.0

    def missing(self) -> str | None:
        return None

    async def fetch(self, url: str, http: WebClient, *, max_bytes: int,
                    in_scope: Callable[[str, str], bool]) -> Page:   # pragma: no cover - 协议
        raise NotImplementedError


def classify_status(status: int, service: str, *, retry_after: str | None = None, detail: str = "",
                    fetch: bool = False) -> WebError:
    """把 HTTP 状态码归类。``detail`` 是后端错误体里的说明，只截一小段，绝不带请求头。"""
    tail = f"：{detail[:200]}" if detail else ""
    if status in (401,) or (status == 403 and not fetch):
        return WebError("auth", f"{service} 鉴权失败（HTTP {status}）{tail}", status=status)
    if status == 402:
        return WebError("quota", f"{service} 余额不足（HTTP 402）{tail}", status=status)
    if status == 403:
        return WebError("blocked", "该网址被读取服务的安全策略拒绝", status=status)
    if status == 404 and fetch:
        return WebError("no_content", "取不到可用内容（站点拒绝、类型不支持或没有正文）", status=status)
    if status == 429:
        return WebError("rate_limited", f"{service} 限流（HTTP 429）", status=status, retry_after=_retry_after(retry_after))
    if status in (408,) or status >= 500:
        return WebError("transient", f"{service} 暂时不可用（HTTP {status}）", status=status)
    return WebError("bad_request", f"{service} 拒绝了请求（HTTP {status}）{tail}", status=status)


def _retry_after(v: str | None) -> float | None:
    try:
        return max(0.0, float(v)) if v is not None else None
    except ValueError:
        return None


def error_detail(resp: Fetched) -> str:
    try:
        body = resp.json()
    except Exception:
        return resp.text()[:200].strip()
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or "")
        return str(err or body.get("message") or body.get("msg") or "")
    return ""


def check(resp: Fetched, service: str, *, fetch: bool = False) -> Any:
    """2xx 返回解析后的 JSON，其余按状态码归类后抛出。"""
    if resp.status >= 300:
        raise classify_status(resp.status, service, retry_after=resp.headers.get("retry-after"),
                              detail=error_detail(resp), fetch=fetch)
    try:
        return resp.json()
    except ValueError:
        raise WebError("transient", f"{service} 返回的不是合法 JSON") from None


# ---------------------------------------------------------------------- 归一化
def normalize_url(url: str) -> str:
    """小写协议与主机、去掉默认端口、utm_* 参数和片段。只用于展示和去重；读取范围的比对用 ``url_key``。"""
    try:
        u = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = (u.hostname or "").lower()
    if u.port and not ((u.scheme == "https" and u.port == 443) or (u.scheme == "http" and u.port == 80)):
        host = f"{host}:{u.port}"
    query = urlencode([(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if not k.lower().startswith("utm_")])
    return urlunsplit((u.scheme.lower(), host, u.path or "/", query, ""))


def url_key(url: str) -> str:
    """读取范围比对用的键：只规范大小写、默认端口和片段，查询串一个字符都不动——在已引用的 URL 后面拼参数就不再匹配。"""
    try:
        u = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = (u.hostname or "").lower()
    if u.port and not ((u.scheme == "https" and u.port == 443) or (u.scheme == "http" and u.port == 80)):
        host = f"{host}:{u.port}"
    return urlunsplit((u.scheme.lower(), host, u.path or "/", u.query, ""))


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def denied_host(host: str, deny: list[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in (x.lower().lstrip(".") for x in deny) if d)


def clean_text(s: Any) -> str:
    """去标签、反转义、压空白。后端给的摘要常带 <em> 高亮。"""
    import html
    import re

    if not s:
        return ""
    s = re.sub(r"<[^>]{0,200}>", "", str(s))
    return " ".join(html.unescape(s).split())


# ---------------------------------------------------------------------- 结果
@dataclass
class SearchOutcome:
    backend: str
    hits: list[SearchHit]
    cached: bool = False
    elapsed_ms: int = 0
    cost_usd: float = 0.0
    billed: bool = False           # 实际发出并完成了一次后端搜索（计入次数）
    filtered: dict[str, int] = field(default_factory=lambda: {"deny": 0, "dup": 0})
    failover: list[str] = field(default_factory=list)   # 换掉的后端及原因


@dataclass
class FetchOutcome:
    backend: str
    page: Page
    elapsed_ms: int = 0
    cost_usd: float = 0.0


class WebService:
    """进程内唯一：后端熔断、并发信号量、缓存都要跨会话共享（缓存键里带会话范围）。"""

    def __init__(self, cfg: WebConfig, *, env: Mapping[str, str] | None = None, client: WebClient | None = None,
                 backends: list[SearchBackend] | None = None, fetcher: FetchBackend | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        from .backends import make_backend, make_fetcher

        self.cfg = cfg
        self.env = os.environ if env is None else env
        self.backends = backends if backends is not None else [make_backend(n, cfg, self.env) for n in cfg.backends]
        self.fetcher = fetcher if fetcher is not None else make_fetcher(cfg, self.env)
        self._client = client
        self.clock = clock
        self.sleep = sleep
        self._broken: dict[str, float] = {}
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._cache: OrderedDict[tuple, tuple[float, SearchOutcome]] = OrderedDict()

    # ------------------------------------------------------------------ 可用性
    @property
    def http(self) -> WebClient:
        if self._client is None:
            self._client = WebClient(timeout=self.cfg.timeout)
        return self._client

    def usable(self) -> list[SearchBackend]:
        return [b for b in self.backends if b.missing() is None]

    def availability(self) -> tuple[str, str]:
        """(最高可用能力, 原因)。原因只说缺什么，不带端点和变量名之外的配置。"""
        if not self.cfg.enabled:
            return "off", "联网工具没有启用（用户配置 [web] enabled = false）"
        if not self.usable():
            why = "；".join(f"{b.name}：{b.missing()}" for b in self.backends) or "没有配置后端"
            return "off", f"没有可用的搜索后端（{why}）"
        if self.fetcher is None or self.fetcher.missing() is not None:
            why = self.fetcher.missing() if self.fetcher is not None else "没有配置"
            return "search", f"网页读取不可用（{why}）"
        return "fetch", ""

    def unpriced(self) -> list[str]:
        """没配单价的后端：费用按 0 计，费用上限对它们不生效。"""
        names = [b.name for b in self.usable() if not b.price and b.name != "searxng"]
        if self.fetcher is not None and self.fetcher.missing() is None and not self.fetcher.price \
                and self.fetcher.name not in ("direct", *names):
            names.append(self.fetcher.name)
        return names

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def _sem(self, name: str) -> asyncio.Semaphore:
        if name not in self._sems:
            self._sems[name] = asyncio.Semaphore(CONCURRENCY)
        return self._sems[name]

    # ------------------------------------------------------------------ 搜索
    def cached(self, scope: str, q: SearchQuery) -> SearchOutcome | None:
        if self.cfg.cache_ttl <= 0:
            return None
        key = self._key(scope, q)
        hit = self._cache.get(key)
        if hit is None:
            return None
        at, out = hit
        if self.clock() - at > self.cfg.cache_ttl:
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return SearchOutcome(out.backend, list(out.hits), cached=True, filtered=dict(out.filtered))

    def _key(self, scope: str, q: SearchQuery) -> tuple:
        return (scope, tuple(b.name for b in self.backends), " ".join(q.q.split()).lower(), q.count, q.freshness,
                (q.site or "").lower())

    async def search(self, q: SearchQuery, *, scope: str = "", question: str | None = None) -> SearchOutcome:
        hit = self.cached(scope, q)
        if hit is not None:
            return hit
        t0 = self.clock()
        failover: list[str] = []
        last: WebError | None = None
        for b in self.backends:
            if (why := b.missing()) is not None:
                failover.append(f"{b.name}：{why}")
                continue
            until = self._broken.get(b.name)
            if until is not None and self.clock() < until:
                failover.append(f"{b.name}：余额不足，暂停使用到 {int(until - self.clock())} 秒后")
                continue
            try:
                hits = await self._try(b, q, question)
            except WebError as e:
                last = e
                if e.kind == "quota":
                    self._broken[b.name] = self.clock() + BREAKER_SECONDS
                if e.kind in ("bad_request", "blocked"):
                    raise
                failover.append(f"{b.name}：{e}")
                continue
            out = self._normalize(b, hits, q)
            out.elapsed_ms = int((self.clock() - t0) * 1000)
            out.billed = True
            out.cost_usd = b.price if hits else 0.0
            out.failover = failover
            if self.cfg.cache_ttl > 0:
                self._cache[self._key(scope, q)] = (self.clock(), out)
                while len(self._cache) > _CACHE_SIZE:
                    self._cache.popitem(last=False)
            return out
        if last is not None and last.kind in ("rate_limited", "transient"):
            raise TransientToolError("；".join(failover))
        hint = ""
        if last is not None and last.kind == "auth":
            hint = "。检查用户配置 [web.backend.*] 的 api_key_env 指向的环境变量"
        raise WebError(last.kind if last else "auth", "所有搜索后端都不可用：" + "；".join(failover) + hint)

    async def _try(self, b: SearchBackend, q: SearchQuery, question: str | None) -> list[SearchHit]:
        attempt = 0
        while True:
            try:
                async with self._sem(b.name):
                    return await b.search(q, self.http, question=question)
            except WebError as e:
                if e.kind not in ("rate_limited", "transient") or attempt >= _RETRIES:
                    raise
                wait = e.retry_after if e.retry_after is not None else 0.5 * 2 ** attempt
                if wait > _MAX_WAIT:   # 让等太久：换下一家比干等划算
                    raise
                attempt += 1
                await self.sleep(wait)

    def _normalize(self, b: SearchBackend, hits: list[SearchHit], q: SearchQuery) -> SearchOutcome:
        seen: set[str] = set()
        keep: list[SearchHit] = []
        deny = dup = 0
        min_score = float(self.cfg.backend.get(b.name, {}).get("min_rerank_score", 0.0) or 0.0)
        for h in hits:
            if not h.url.lower().startswith(("https://", "http://")):
                continue
            h.url = normalize_url(h.url)
            if denied_host(host_of(h.url), self.cfg.deny_domains):
                deny += 1
                continue
            if h.url in seen:
                dup += 1
                continue
            if min_score and h.score is not None and h.score < min_score:
                continue
            seen.add(h.url)
            h.title = clean_text(h.title) or h.url
            h.snippet = clean_text(h.snippet)
            h.content = clean_text(h.content) or None
            h.site = clean_text(h.site) or host_of(h.url)
            keep.append(h)
        if any(h.score is not None for h in keep):
            keep.sort(key=lambda h: -(h.score if h.score is not None else -1.0))
        return SearchOutcome(b.name, keep[: q.count], filtered={"deny": deny, "dup": dup})

    # ------------------------------------------------------------------ 读取
    async def fetch(self, url: str, *, in_scope: Callable[[str, str], bool], max_bytes: int | None = None) -> FetchOutcome:
        f = self.fetcher
        if f is None or (why := f.missing()) is not None:
            raise WebError("auth", f"网页读取不可用：{why if f is not None else '没有配置读取后端'}")
        t0 = self.clock()
        attempt = 0
        while True:
            try:
                async with self._sem(f"fetch:{f.name}"):
                    page = await f.fetch(url, self.http, max_bytes=max_bytes or self.cfg.fetch_max_bytes, in_scope=in_scope)
                break
            except WebError as e:
                if e.kind not in ("rate_limited", "transient") or attempt >= _RETRIES:
                    if e.kind in ("rate_limited", "transient"):
                        raise TransientToolError(str(e)) from None
                    raise
                wait = e.retry_after if e.retry_after is not None else 0.5 * 2 ** attempt
                if wait > _MAX_WAIT:
                    raise TransientToolError(str(e)) from None
                attempt += 1
                await self.sleep(wait)
        return FetchOutcome(f.name, page, int((self.clock() - t0) * 1000), f.price if page.content else 0.0)


__all__ = ["FRESHNESS_DAYS", "FetchBackend", "FetchOutcome", "Page", "SearchBackend", "SearchHit", "SearchOutcome",
           "SearchQuery", "WebService", "check", "classify_status", "clean_text", "denied_host", "host_of",
           "normalize_url", "url_key"]
