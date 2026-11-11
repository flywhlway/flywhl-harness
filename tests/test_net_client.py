"""WebClient 的出口核验：地址、协议、DNS 重绑定、重定向、响应上限、类型白名单；外传检查。

一个真实请求都不发：传输层是 MockTransport；测钉 IP 的那条换了一个只记录参数的假网络后端。
"""

from __future__ import annotations

import gzip

import httpcore
import httpx
import pytest

from flywhl_harness.net.client import USER_AGENT, WebClient, WebError, _PinnedBackend
from flywhl_harness.net.exfil import leaked, secret_values

from .webkit import PUBLIC, Net, Resolver

ANY = lambda host, url: True   # noqa: E731 - 范围判定交给调用方，这里只测核验


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "::1", "10.0.0.5", "172.16.3.4", "192.168.1.1", "100.64.0.1",   # 回环、内网、CGNAT
    "169.254.169.254", "fe80::1", "0.0.0.0", "224.0.0.1",                         # 云元数据、链路本地、未指定、组播
    "64:ff9b::a9fe:a9fe", "::ffff:127.0.0.1", "::ffff:10.0.0.1",                  # NAT64 合成、IPv4 映射
    "fd00::1", "fec0::1",                                                          # IPv6 唯一本地、站点本地
])
async def test_non_public_addresses_are_refused_before_any_request(ip):
    net = Net().on("GET", "https://", httpx.Response(200, text="不该到这里"))
    client = net.client(Resolver({"evil.test": ip}))
    with pytest.raises(WebError) as e:
        await client.request("GET", "https://evil.test/", in_scope=ANY)
    assert e.value.kind == "blocked"
    assert net.requests == [] and client.calls == 0


@pytest.mark.parametrize("url", ["https://127.0.0.1/", "https://[::1]/", "https://169.254.169.254/latest/meta-data/",
                                 "https://[64:ff9b::a9fe:a9fe]/"])
async def test_ip_literal_urls_are_vetted_without_resolving(url):
    net = Net()
    resolver = Resolver()
    with pytest.raises(WebError, match="联网工具不连这类地址"):
        await net.client(resolver).request("GET", url, in_scope=ANY)
    assert resolver.calls == [] and net.requests == []


async def test_scheme_and_userinfo_rules():
    net = Net().on("GET", "http://searx.lan", httpx.Response(200, json={}))
    client = net.client(Resolver({"searx.lan": "192.168.1.9"}))
    for url, kind in [("http://example.com/", "blocked"), ("ftp://example.com/", "blocked"),
                      ("https://user:pw@example.com/", "blocked"), ("https:///nohost", "bad_request"),
                      ("https://example.com:99999/", "bad_request")]:
        with pytest.raises(WebError) as e:
            await client.request("GET", url, in_scope=ANY)
        assert e.value.kind == kind, url
    # 自建后端：http 与内网地址只对显式列出的主机放行
    with pytest.raises(WebError):
        await client.request("GET", "http://searx.lan/search", in_scope=ANY, allow_http=["searx.lan"])
    ok = await client.request("GET", "http://searx.lan/search", in_scope=ANY, allow_http=["searx.lan"],
                              allow_private=["searx.lan"])
    assert ok.status == 200
    assert len(net.requests) == 1


async def test_allow_private_never_reaches_metadata_or_link_local():
    for ip in ("169.254.169.254", "fe80::1"):
        client = Net().client(Resolver({"searx.lan": ip}))
        with pytest.raises(WebError, match="联网工具只连公网地址"):
            await client.request("GET", "https://searx.lan/", in_scope=ANY, allow_private=["searx.lan"])


async def test_out_of_scope_host_is_refused():
    net = Net()
    with pytest.raises(WebError, match="不在允许的范围内"):
        await net.client().request("GET", "https://other.test/", in_scope=lambda host, url: host == "api.test")
    assert net.requests == []


class _RecordingBackend(httpcore.AsyncNetworkBackend):
    """只记录 connect_tcp 收到的地址，然后拒绝连接：证明连接用的是核过的 IP，而不是再按主机名解析一次。"""

    def __init__(self) -> None:
        self.hosts: list[str] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.hosts.append(host)
        raise httpcore.ConnectError("测试里不真的连")

    async def sleep(self, seconds):   # pragma: no cover - 不会走到
        pass


async def test_dns_is_resolved_once_and_the_connection_is_pinned_to_the_vetted_ip():
    answers = iter([PUBLIC, "10.0.0.1"])   # 重绑定：第一次给公网，第二次给内网
    resolver = Resolver({"rebind.test": lambda: next(answers)})
    backend = _RecordingBackend()
    client = WebClient(resolve=resolver, backend=backend)
    with pytest.raises(WebError) as e:
        await client.request("GET", "https://rebind.test/", in_scope=ANY)
    await client.aclose()
    assert e.value.kind == "transient"
    assert resolver.calls == ["rebind.test"]       # 只解析一次
    assert backend.hosts == [PUBLIC]                # 连的是核过的 IP


async def test_pinned_backend_refuses_hosts_that_were_never_vetted():
    with pytest.raises(httpcore.ConnectError, match="没有经过出口核验"):
        await _PinnedBackend(_RecordingBackend()).connect_tcp("example.com", 443)


async def test_mixed_answers_keep_only_public_addresses():
    backend = _RecordingBackend()
    client = WebClient(resolve=Resolver({"mixed.test": ["10.0.0.1", PUBLIC, "127.0.0.1"]}), backend=backend)
    with pytest.raises(WebError):
        await client.request("GET", "https://mixed.test/", in_scope=ANY)
    await client.aclose()
    assert backend.hosts == [PUBLIC]


async def test_each_redirect_hop_is_vetted_again():
    net = (Net().on("GET", "https://start.test/", httpx.Response(302, headers={"location": "https://inner.test/x"}))
           .on("GET", "https://inner.test/", httpx.Response(200, text="内网数据")))
    with pytest.raises(WebError, match="联网工具只连公网地址"):
        await net.client(Resolver({"inner.test": "10.1.2.3"})).request("GET", "https://start.test/", in_scope=ANY,
                                                                         redirects=3)
    assert [str(r.url) for r in net.requests] == ["https://start.test/"]


async def test_redirect_hops_go_through_the_callers_scope():
    net = (Net().on("GET", "https://a.test/", httpx.Response(301, headers={"location": "https://b.test/leak?d=1"}))
           .on("GET", "https://b.test/", httpx.Response(200, text="x")))
    seen: list[str] = []

    def scope(host: str, url: str) -> bool:
        seen.append(url)
        return host == "a.test"

    with pytest.raises(WebError, match="b.test 不在允许的范围内"):
        await net.client().request("GET", "https://a.test/", in_scope=scope, redirects=3)
    assert seen == ["https://a.test/", "https://b.test/leak?d=1"]
    assert len(net.requests) == 1


async def test_redirects_are_not_followed_unless_asked_and_are_capped():
    loop = Net().on("GET", "https://loop.test/", httpx.Response(302, headers={"location": "/again"}))
    with pytest.raises(WebError, match="重定向超过 0 次"):
        await loop.client().request("GET", "https://loop.test/", in_scope=ANY)
    with pytest.raises(WebError, match="重定向超过 3 次"):
        await loop.client().request("GET", "https://loop.test/", in_scope=ANY, redirects=3)
    assert len(loop.requests) == 1 + 4


async def test_post_becomes_get_after_303():
    net = (Net().on("POST", "https://a.test/", httpx.Response(303, headers={"location": "/done"}))
           .on("GET", "https://a.test/done", httpx.Response(200, json={"ok": True})))
    out = await net.client().request("POST", "https://a.test/", json={"q": 1}, in_scope=ANY, redirects=1)
    assert out.json() == {"ok": True} and out.hops == ["https://a.test/", "https://a.test/done"]
    assert net.requests[1].method == "GET" and net.requests[1].content == b""


async def test_size_cap_counts_decompressed_bytes():
    bomb = gzip.compress(b"0" * 3_000_000)   # 压缩后几 KB，解压后 3 MB
    assert len(bomb) < 50_000
    net = Net().on("GET", "https://zip.test/", httpx.Response(200, content=bomb, headers={
        "content-encoding": "gzip", "content-type": "text/plain"}))
    with pytest.raises(WebError) as e:
        await net.client().request("GET", "https://zip.test/", in_scope=ANY, max_bytes=1_000_000)
    assert e.value.kind == "too_large"
    cut = await net.client().request("GET", "https://zip.test/", in_scope=ANY, max_bytes=1_000_000, truncate=True)
    assert cut.truncated and len(cut.body) == 1_000_000


async def test_content_type_allowlist():
    net = (Net().on("GET", "https://a.test/pdf", httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"}))
           .on("GET", "https://a.test/md", httpx.Response(200, text="# x", headers={"content-type": "text/markdown; charset=utf-8"})))
    client = net.client()
    with pytest.raises(WebError) as e:
        await client.request("GET", "https://a.test/pdf", in_scope=ANY, accept=("text/html", "text/*"))
    assert e.value.kind == "bad_type"
    md = await client.request("GET", "https://a.test/md", in_scope=ANY, accept=("text/html", "text/*"))
    assert md.text() == "# x"


async def test_timeouts_and_connection_errors_are_transient():
    def boom(request):
        raise httpx.ReadTimeout("慢", request=request)

    with pytest.raises(WebError) as e:
        await Net().on("GET", "https://slow.test/", boom).client().request("GET", "https://slow.test/", in_scope=ANY)
    assert e.value.kind == "transient" and "超时" in str(e.value)
    with pytest.raises(WebError) as e:
        await Net().client(Resolver({"gone.test": OSError("nodename nor servname provided")})).request(
            "GET", "https://gone.test/", in_scope=ANY)
    assert e.value.kind == "transient"


async def test_requests_carry_a_fixed_user_agent_and_no_ambient_headers(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")   # 不读代理环境变量：经代理就钉不住 IP
    net = Net().on("GET", "https://a.test/", httpx.Response(200, text="ok", headers={"set-cookie": "s=1"}))
    client = net.client()
    await client.request("GET", "https://a.test/", in_scope=ANY)
    await client.request("GET", "https://a.test/", in_scope=ANY)
    for r in net.requests:
        assert r.headers["user-agent"] == USER_AGENT
        assert "cookie" not in r.headers and "referer" not in r.headers


async def test_charset_from_content_type():
    body = "你好".encode("gbk")
    net = Net().on("GET", "https://a.test/", httpx.Response(200, content=body, headers={"content-type": "text/plain; charset=gbk"}))
    assert (await net.client().request("GET", "https://a.test/", in_scope=ANY)).text() == "你好"


# ---------------------------------------------------------------------- 外传检查
SENTINEL = "sentinel-value-0042"   # 假的“密钥值”：只用来证明比对会命中


def test_leak_check_hits_secret_env_values_raw_and_url_encoded():
    env = {"MY_SERVICE_TOKEN": SENTINEL, "PATH": "/usr/bin:/bin", "SHORT_KEY": "abc", "GIT_AUTHOR_NAME": "someone-long-name"}
    assert secret_values(env) == [SENTINEL]
    assert leaked(f"httpx {SENTINEL}", env=env)
    assert leaked("https://x.test/?d=" + SENTINEL.replace("-", "%2D"), env=env)
    assert leaked("search someone-long-name", env=env) is None   # 不算密钥的变量名
    assert leaked("abc def", env=env) is None                     # 太短的值不比对


@pytest.mark.parametrize("text", ["sk-" + "a" * 24, "ghp_" + "b" * 30, "github_pat_" + "c" * 24,
                                  "AKIA" + "ABCDEFGHIJKLMNOP", "xoxb-" + "1234567890-abc"])
def test_leak_check_hits_well_known_key_prefixes(text):
    assert leaked(f"look up {text}", env={}) == "包含形似 API 密钥的字符串"


def test_leak_check_heuristics_for_random_tokens():
    token = "Zx8Qm2LpR7vT4nWc9Ks1Yb6Hd3Fg5Ja0"
    assert leaked(f"https://x.test/?t={token}", env={}) == "包含形似令牌的长随机串"
    assert leaked(f"https://x.test/?t={token}", env={}, heuristics=False) is None
    # 提交号、哈希、普通长词都不算
    assert leaked("https://github.com/encode/httpx/commit/" + "0123456789abcdef" * 3, env={}) is None
    assert leaked("https://docs.python.org/3/library/asyncio-eventloop.html", env={}) is None
