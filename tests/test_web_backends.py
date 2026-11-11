"""搜索 / 读取后端：响应映射、错误表逐行、重试与 Retry-After、故障转移与熔断、缓存、计费口径、正文抽取。"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from flywhl_harness.config import WebConfig
from flywhl_harness.net.backends.bocha import BochaSearch
from flywhl_harness.net.backends.deepseek import PRICE, DeepSeekFetch, DeepSeekSearch
from flywhl_harness.net.backends.direct import DirectFetch
from flywhl_harness.net.backends.searxng import SearxngSearch
from flywhl_harness.net.client import WebError
from flywhl_harness.net.extract import html_to_text, to_text
from flywhl_harness.net.search import SearchQuery, WebService, normalize_url, url_key
from flywhl_harness.tools.base import TransientToolError

from .webkit import BOCHA, DS_FETCH, DS_SEARCH, FAKE_KEY, Net, Resolver, Sleeps, ds_search, fixture

ENV = {"DEEPSEEK_API_KEY": FAKE_KEY, "BOCHA_API_KEY": FAKE_KEY}
Q = SearchQuery("httpx latest version", 5)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def service(net: Net, *, backends=("deepseek",), sleeps=None, clock=None, resolver=None, **over) -> WebService:
    cfg = WebConfig(enabled=True, backends=list(backends), **over)
    return WebService(cfg, env=ENV, client=net.client(resolver), sleep=sleeps if sleeps is not None else Sleeps(),
                      clock=clock or Clock())


def err(status: int, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=fixture("deepseek", "errors")[str(status)], headers=headers)


# ---------------------------------------------------------------------- 映射
async def test_deepseek_search_mapping_and_normalization():
    net = Net().on("POST", DS_SEARCH, ds_search())
    out = await service(net, deny_domains=["spam.example"]).search(Q)
    body = net.bodies(DS_SEARCH)[0]
    assert body == {"query": "httpx latest version", "count": 5, "need_content": False}   # 不发 ori_question
    assert net.requests[0].headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert [h.url for h in out.hits] == ["https://pypi.org/project/httpx/", "https://www.python-httpx.org/changelog/?ref=top"]
    first = out.hits[1]
    assert first.title == "httpx 0.28.1 — Changelog"          # 去掉 <em>
    assert first.published == "2024-12-06" and first.authority == 3 and first.site == "HTTPX"
    assert first.content.startswith("HTTPX 的更新日志")         # summary 给模型
    assert out.filtered == {"deny": 1, "dup": 1}               # 镜像站被 deny_domains 滤掉，#history 去重
    assert out.billed and out.cost_usd == PRICE and not out.cached


def test_deepseek_compose_puts_filters_into_the_query_and_refuses_overflow():
    b = DeepSeekSearch({}, ENV, today=lambda: dt.date(2026, 11, 12))
    assert b.compose(SearchQuery("httpx", 5, "week", "pypi.org")) == "httpx site:pypi.org after:2026-11-05"
    assert b.compose(SearchQuery("x" * 128, 5)) == "x" * 128
    with pytest.raises(WebError) as e:
        b.compose(SearchQuery("x" * 120, 5, "year", "docs.python.org"))
    assert e.value.kind == "bad_request" and "超过 DeepSeek 的上限 128" in str(e.value)


async def test_ori_question_only_when_opted_in():
    net = Net().on("POST", DS_SEARCH, ds_search())
    b = DeepSeekSearch({"send_question": True}, ENV)
    await b.search(Q, net.client(), question="用户的原始问题")
    assert net.bodies()[0]["ori_question"] == "用户的原始问题"


async def test_custom_endpoint_strips_v1_and_scopes_to_its_host():
    net = Net().on("POST", "https://ds-proxy.example/v1/tools/search", ds_search([]))
    b = DeepSeekSearch({"endpoint": "https://ds-proxy.example/v1/"}, ENV)
    assert b.hosts == ("ds-proxy.example",) and b.in_scope("ds-proxy.example", "") and not b.in_scope("api.deepseek.com", "")
    assert await b.search(Q, net.client()) == []


async def test_bocha_mapping():
    net = Net().on("POST", BOCHA, httpx.Response(200, json=fixture("bocha", "search")))
    hits = await BochaSearch({}, ENV).search(SearchQuery("httpx 最新版本", 3, "month", "oschina.net"), net.client())
    assert net.bodies()[0] == {"query": "httpx 最新版本 site:oschina.net", "freshness": "oneMonth", "summary": True, "count": 3}
    assert [(h.title, h.site, h.published) for h in hits] == [("httpx 发布 0.28.1", "开源中国", "2024-12-07"),
                                                               ("httpx 文档", "HTTPX", None)]
    assert hits[0].content.startswith("开源中国报道") and hits[1].content is None


async def test_bocha_errors_inside_a_200_body_and_403_as_quota():
    for resp, kind in [(httpx.Response(200, json={"code": 401, "msg": "Invalid API KEY"}), "auth"),
                       (httpx.Response(200, json={"code": 403, "msg": "You do not have enough money"}), "quota"),
                       (httpx.Response(403, json={"message": "You do not have enough money"}), "quota"),
                       (httpx.Response(429, json={"message": "limit"}), "rate_limited")]:
        net = Net().on("POST", BOCHA, resp)
        with pytest.raises(WebError) as e:
            await BochaSearch({}, ENV).search(Q, net.client())
        assert e.value.kind == kind


async def test_searxng_mapping_private_opt_in_and_403_hint():
    data = fixture("searxng", "search")
    net = Net().on("GET", "http://searx.lan:8080/search", httpx.Response(200, json=data))
    resolver = Resolver({"searx.lan": "192.168.1.20"})
    lan = SearxngSearch({"endpoint": "http://searx.lan:8080/search", "allow_private": True})
    hits = await lan.search(SearchQuery("httpx", 5, "year"), net.client(resolver))
    assert str(net.requests[0].url) == "http://searx.lan:8080/search?q=httpx&format=json&time_range=year"
    assert hits[0].url == "https://github.com/encode/httpx/releases" and hits[0].published == "2024-12-06"
    # 没写 allow_private：内网地址和 http 都不放行
    with pytest.raises(WebError):
        await SearxngSearch({"endpoint": "http://searx.lan:8080/search"}).search(Q, net.client(resolver))
    assert len(net.requests) == 1
    forbidden = Net().on("GET", "https://searx.example/", httpx.Response(403, text="Forbidden"))
    with pytest.raises(WebError, match="search.formats 里打开 json"):
        await SearxngSearch({"endpoint": "https://searx.example/search"}).search(Q, forbidden.client())
    assert SearxngSearch({}).missing() == "没有配置 endpoint"


async def test_searxng_results_are_sorted_by_score():
    net = Net().on("GET", "https://searx.example/", httpx.Response(200, json=fixture("searxng", "search")))
    out = await service(net, backends=("searxng",), backend={"searxng": {"endpoint": "https://searx.example/search"}}).search(Q)
    assert [h.url for h in out.hits] == ["https://pypi.org/project/httpx/", "https://github.com/encode/httpx/releases"]
    assert out.hits[0].site == "pypi.org"   # 没有站点名时用主机名


def test_url_normalization_keeps_the_query_for_scope_matching():
    assert normalize_url("HTTPS://Example.COM:443/a?utm_medium=x&b=1#frag") == "https://example.com/a?b=1"
    assert url_key("HTTPS://Example.COM:443/a?utm_medium=x&b=1#frag") == "https://example.com/a?utm_medium=x&b=1"
    assert url_key("https://example.com") == "https://example.com/"
    assert url_key("https://example.com/a?b=1") != url_key("https://example.com/a?b=1&leak=2")


# ---------------------------------------------------------------------- 错误表
@pytest.mark.parametrize("status,kind", [(400, "bad_request"), (413, "bad_request")])
async def test_bad_requests_are_not_retried_and_not_failed_over(status, kind):
    net = Net().on("POST", DS_SEARCH, err(status)).on("POST", BOCHA, httpx.Response(200, json=fixture("bocha", "search")))
    sleeps = Sleeps()
    with pytest.raises(WebError) as e:
        await service(net, backends=("deepseek", "bocha"), sleeps=sleeps).search(Q)
    assert e.value.kind == kind and "HTTP" in str(e.value)
    assert len(net.requests) == 1 and sleeps == []


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failures_fail_over_without_retry(status):
    net = Net().on("POST", DS_SEARCH, err(status)).on("POST", BOCHA, httpx.Response(200, json=fixture("bocha", "search")))
    sleeps = Sleeps()
    out = await service(net, backends=("deepseek", "bocha"), sleeps=sleeps).search(Q)
    assert out.backend == "bocha" and "鉴权失败" in out.failover[0] and sleeps == []


async def test_auth_failure_everywhere_points_at_the_key_variable():
    net = Net().on("POST", DS_SEARCH, err(401))
    with pytest.raises(WebError) as e:
        await service(net).search(Q)
    assert e.value.kind == "auth" and "api_key_env" in str(e.value)
    assert FAKE_KEY not in str(e.value)


async def test_quota_trips_a_breaker_for_ten_minutes():
    clock = Clock()
    net = Net().on("POST", DS_SEARCH, err(402)).on("POST", BOCHA, httpx.Response(200, json=fixture("bocha", "search")))
    svc = service(net, backends=("deepseek", "bocha"), clock=clock, cache_ttl=0)
    first = await svc.search(Q)
    assert first.backend == "bocha" and "余额不足" in first.failover[0]
    second = await svc.search(Q)
    assert "暂停使用" in second.failover[0]
    assert sum(str(r.url) == DS_SEARCH for r in net.requests) == 1   # 熔断期间不再调 DeepSeek
    clock.t += 601
    await svc.search(Q)
    assert sum(str(r.url) == DS_SEARCH for r in net.requests) == 2


@pytest.mark.parametrize("status", [408, 500, 504])
async def test_transient_errors_retry_with_backoff_then_succeed(status):
    net = Net().on("POST", DS_SEARCH, [err(status), err(status), ds_search()])
    sleeps = Sleeps()
    out = await service(net, sleeps=sleeps).search(Q)
    assert out.hits and sleeps == [0.5, 1.0] and len(net.requests) == 3


async def test_retry_after_is_honoured_and_long_waits_fail_over():
    net = Net().on("POST", DS_SEARCH, [err(429, {"retry-after": "2"}), ds_search()])
    sleeps = Sleeps()
    assert (await service(net, sleeps=sleeps).search(Q)).backend == "deepseek"
    assert sleeps == [2.0]
    slow = Net().on("POST", DS_SEARCH, err(429, {"retry-after": "30"})).on(
        "POST", BOCHA, httpx.Response(200, json=fixture("bocha", "search")))
    sleeps = Sleeps()
    out = await service(slow, backends=("deepseek", "bocha"), sleeps=sleeps).search(Q)
    assert out.backend == "bocha" and sleeps == [] and "限流" in out.failover[0]


async def test_all_backends_transient_raises_transient_tool_error():
    net = Net().on("POST", DS_SEARCH, err(500)).on("POST", BOCHA, httpx.Response(503, json={}))
    with pytest.raises(TransientToolError) as e:
        await service(net, backends=("deepseek", "bocha")).search(Q)
    assert "deepseek" in str(e.value) and "bocha" in str(e.value)


async def test_invalid_json_is_transient():
    net = Net().on("POST", DS_SEARCH, httpx.Response(200, content=b"<html>", headers={"content-type": "application/json"}))
    with pytest.raises(TransientToolError):
        await service(net).search(Q)


async def test_missing_keys_are_skipped_with_a_reason():
    net = Net().on("POST", BOCHA, httpx.Response(200, json=fixture("bocha", "search")))
    cfg = WebConfig(enabled=True, backends=["deepseek", "bocha"])
    svc = WebService(cfg, env={"BOCHA_API_KEY": FAKE_KEY}, client=net.client(), sleep=Sleeps())
    out = await svc.search(Q)
    assert out.backend == "bocha" and out.failover == ["deepseek：缺少密钥（环境变量 DEEPSEEK_API_KEY 为空）"]
    none = WebService(WebConfig(enabled=True), env={}, client=net.client())
    assert none.availability() == ("off", "没有可用的搜索后端（deepseek：缺少密钥（环境变量 DEEPSEEK_API_KEY 为空））")


async def test_empty_results_are_counted_but_not_charged():
    net = Net().on("POST", DS_SEARCH, ds_search([]))
    out = await service(net).search(Q)
    assert out.hits == [] and out.billed and out.cost_usd == 0.0


# ---------------------------------------------------------------------- 缓存
async def test_cache_is_per_session_scope_and_expires():
    clock = Clock()
    net = Net().on("POST", DS_SEARCH, ds_search())
    svc = service(net, clock=clock)
    await svc.search(Q, scope="s1")
    again = await svc.search(SearchQuery("  HTTPX   latest version ", 5), scope="s1")
    assert again.cached and again.cost_usd == 0.0 and not again.billed
    await svc.search(Q, scope="s2")                         # 别的会话不共享
    await svc.search(SearchQuery(Q.q, 3), scope="s1")       # 参数不同不算命中
    assert len(net.requests) == 3
    clock.t += 601
    assert svc.cached("s1", Q) is None


# ---------------------------------------------------------------------- 读取
async def test_deepseek_fetch_and_its_error_rows():
    net = Net().on("POST", DS_FETCH, httpx.Response(200, json=fixture("deepseek", "fetch")))
    page = await DeepSeekFetch({}, ENV).fetch("https://www.python-httpx.org/changelog/", net.client(), max_bytes=10_000,
                                              in_scope=lambda h, u: False)
    assert page.title == "Changelog - HTTPX" and "0.28.1" in page.content
    assert net.bodies()[0] == {"url": "https://www.python-httpx.org/changelog/"}
    for status, kind in [(403, "blocked"), (404, "no_content"), (401, "auth"), (402, "quota"), (400, "bad_request")]:
        n = Net().on("POST", DS_FETCH, err(status))
        with pytest.raises(WebError) as e:
            await DeepSeekFetch({}, ENV).fetch("https://a.test/", n.client(), max_bytes=10_000, in_scope=lambda h, u: False)
        assert e.value.kind == kind, status
    empty = Net().on("POST", DS_FETCH, httpx.Response(200, json={"title": "x", "content": "  "}))
    with pytest.raises(WebError) as e:
        await DeepSeekFetch({}, ENV).fetch("https://a.test/", empty.client(), max_bytes=10_000, in_scope=lambda h, u: False)
    assert e.value.kind == "no_content"


async def test_service_fetch_retries_then_reports_transient():
    net = Net().on("POST", DS_FETCH, [err(500), err(500), err(500)])
    sleeps = Sleeps()
    with pytest.raises(TransientToolError):
        await service(net, sleeps=sleeps).fetch("https://a.test/", in_scope=lambda h, u: True)
    assert sleeps == [0.5, 1.0]
    ok = Net().on("POST", DS_FETCH, httpx.Response(200, json=fixture("deepseek", "fetch")))
    out = await service(ok).fetch("https://a.test/", in_scope=lambda h, u: True)
    assert out.backend == "deepseek" and out.cost_usd == PRICE


async def test_direct_fetch_extracts_text_and_maps_statuses():
    doc = """<html><head><title>Release notes</title><style>.x{}</style><script>alert(1)</script></head>
    <body><nav>菜单</nav><h1>httpx</h1><p>Version <b>0.28.1</b> &amp; more</p><ul><li>one</li><li>two</li></ul>
    <a href="https://elsewhere.example/">链接文字</a><footer>版权</footer></body></html>"""
    net = Net().on("GET", "https://site.test/ok", httpx.Response(200, text=doc, headers={"content-type": "text/html; charset=utf-8"}))
    page = await DirectFetch().fetch("https://site.test/ok", net.client(), max_bytes=100_000, in_scope=lambda h, u: True)
    assert page.title == "Release notes"
    assert page.content == "# httpx\n\nVersion 0.28.1 & more\n\n- one\n\n- two\n\n链接文字"
    assert "alert" not in page.content and "菜单" not in page.content and "elsewhere.example" not in page.content
    for status, kind in [(403, "blocked"), (451, "blocked"), (404, "no_content"), (500, "transient"), (429, "transient")]:
        n = Net().on("GET", "https://site.test/", httpx.Response(status, text="x", headers={"content-type": "text/html"}))
        with pytest.raises(WebError) as e:
            await DirectFetch().fetch("https://site.test/", n.client(), max_bytes=1000, in_scope=lambda h, u: True)
        assert e.value.kind == kind, status
    blank = Net().on("GET", "https://site.test/", httpx.Response(200, text="<script>x()</script>", headers={"content-type": "text/html"}))
    with pytest.raises(WebError, match="没有可读的正文"):
        await DirectFetch().fetch("https://site.test/", blank.client(), max_bytes=1000, in_scope=lambda h, u: True)


async def test_direct_fetch_truncates_instead_of_failing():
    net = Net().on("GET", "https://site.test/", httpx.Response(200, text="a" * 5000, headers={"content-type": "text/plain"}))
    page = await DirectFetch().fetch("https://site.test/", net.client(), max_bytes=1000, in_scope=lambda h, u: True)
    assert page.truncated and len(page.content) == 1000


def test_to_text_variants():
    assert to_text("<a><b>x</b> y</a>", "application/xml") == ("", "x  y")
    assert to_text("  plain  ", "text/plain") == ("", "plain")
    title, body = html_to_text("<title> A  B </title><pre>code\n  indented</pre>")
    assert title == "A B" and "code" in body


@pytest.mark.parametrize("backend,url", [("deepseek", DS_SEARCH), ("bocha", BOCHA)])
async def test_recorded_web_fixtures_parse(backend, url):
    """tests/live/record_web.py 录下的真实响应体：映射之后每条都有标题和 http(s) 网址。没录过就跳过。"""
    from .webkit import FIXTURES

    path = FIXTURES / backend / "recorded_search.json"
    if not path.exists():
        pytest.skip("还没录制真实响应")
    net = Net().on("POST", url, httpx.Response(200, json=fixture(backend, "recorded_search")))
    cls = DeepSeekSearch if backend == "deepseek" else BochaSearch
    hits = await cls({}, ENV).search(SearchQuery("Python 最新稳定版本", 5), net.client())
    assert hits and all(h.title and h.url.startswith(("https://", "http://")) for h in hits)


async def test_recorded_deepseek_fetch_parses():
    from .webkit import FIXTURES

    if not (FIXTURES / "deepseek" / "recorded_fetch.json").exists():
        pytest.skip("还没录制真实响应")
    net = Net().on("POST", DS_FETCH, httpx.Response(200, json=fixture("deepseek", "recorded_fetch")))
    page = await DeepSeekFetch({}, ENV).fetch("https://www.python.org/downloads/", net.client(), max_bytes=2_000_000,
                                              in_scope=lambda h, u: False)
    assert "Python" in page.content
