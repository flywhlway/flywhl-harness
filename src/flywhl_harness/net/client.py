"""联网工具唯一的出站 HTTP 入口：每一跳都核验协议、地址和范围，连接钉在核过的 IP 上。

harness 进程里的 HTTP 不经过 bash 沙箱和出站代理（``tools/egress.py`` 管的是沙箱里的命令），所以联网工具必须自带核验，
而且只能有这一份实现：

=================  ==========================================================================================
协议               只允许 https；``http`` 只给显式放行的自建后端（``allow_http``），带用户名密码的 URL 一律拒绝
地址               解析后逐个按 ``address_scope`` 判定，只放行公网；回环、内网、链路本地、云元数据、NAT64 合成地址、
                   IPv4 映射的 IPv6 都按它们指向的地址判定。``allow_private`` 只对列出的自建后端主机放行内网和回环，
                   云元数据和链路本地无论如何不放行
DNS 重绑定         解析一次、核验一次，连接用核过的 IP（自定义 httpcore 网络后端），SNI 和 Host 仍是原主机名
重定向             不自动跟随；调用方给了 ``redirects`` 才手动跟随，每一跳重走上面三项和调用方的范围判定
响应               流式读取，按解压后的字节计上限（防压缩炸弹）；连接、读取分别超时；可限定 Content-Type
请求头             固定 UA，不带 Referer，不收也不发 Cookie；不读 HTTP(S)_PROXY 环境变量（经代理就钉不住 IP）
=================  ==========================================================================================
"""

from __future__ import annotations

import codecs
import http.cookiejar
import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpcore
import httpx

from .. import __version__
from ..tools.egress import EgressDenied, IPAddress, _getaddrinfo, address_scope, vet_host

USER_AGENT = f"flywhl-harness/{__version__}"
_REDIRECTS = frozenset({301, 302, 303, 307, 308})

Resolver = Callable[[str, int], Awaitable[list[IPAddress]]]


class WebError(Exception):
    """联网请求失败。``kind`` 决定重试还是换后端，见 ``net/search.py`` 的错误表。"""

    def __init__(self, kind: str, message: str, *, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after


@dataclass
class Fetched:
    status: int
    url: str                       # 最后一跳的 URL
    headers: dict[str, str]
    body: bytes
    truncated: bool = False        # 超过字节上限被截断（只在 truncate=True 时出现）
    hops: list[str] = field(default_factory=list)

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    def json(self) -> Any:
        import json

        return json.loads(self.body.decode("utf-8", "replace"))

    def text(self) -> str:
        enc = "utf-8"
        for part in self.headers.get("content-type", "").split(";")[1:]:
            k, _, v = part.strip().partition("=")
            if k.lower() == "charset" and v:
                enc = _text_charset(v.strip("\"' "))
        return self.body.decode(enc, "replace")


# 网页实际会用的字符集。响应头里的 charset 由对方控制：punycode、idna 这类编码解 2MB 正文是平方级的，
# unicode-escape、utf-7 会把转义序列还原成另一段文字，都不认，按 utf-8 解
_CHARSETS = {"utf-8", "utf-16", "utf-16-le", "utf-16-be", "ascii", "gbk", "gb2312", "gb18030", "big5", "big5hkscs",
             "shift_jis", "cp932", "euc_jp", "iso2022_jp", "euc_kr", "cp949", "koi8-r", "koi8-u"}


def _text_charset(name: str) -> str:
    try:
        canon = codecs.lookup(name).name
    except LookupError:
        return "utf-8"
    ok = canon in _CHARSETS or canon.startswith(("iso8859-", "cp125"))
    return canon if ok else "utf-8"


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    """把主机名换成核过的 IP 再建 TCP 连接。TLS 握手的 SNI 与证书校验用的是 URL 里的主机名（httpcore 另传），不受影响。
    没核过的主机直接拒绝：连接池不会替你按名字再解析一次。"""

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None):
        self.inner = inner or httpcore.AnyIOBackend()
        self.pins: dict[str, list[str]] = {}

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
                          socket_options: Iterable[Any] | None = None) -> httpcore.AsyncNetworkStream:
        ips = self.pins.get(host.lower().rstrip("."))
        if not ips:
            raise httpcore.ConnectError(f"{host} 没有经过出口核验，拒绝连接")
        error: Exception | None = None
        for ip in ips:
            try:
                return await self.inner.connect_tcp(ip, port, timeout=timeout, local_address=local_address,
                                                    socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as e:
                error = e
        raise httpcore.ConnectError(f"连接 {host}:{port} 失败：{error}")

    async def connect_unix_socket(self, *a: Any, **kw: Any) -> httpcore.AsyncNetworkStream:   # pragma: no cover
        raise httpcore.ConnectError("联网工具不连 Unix 套接字")

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)


class _PinnedTransport(httpx.AsyncHTTPTransport):
    def __init__(self, backend: _PinnedBackend, **kw: Any):
        super().__init__(trust_env=False, retries=0, **kw)
        pool = self._pool
        # httpcore 的连接池在建连时读这个属性；以后版本改名了就拒绝启动，而不是悄悄退回按名字解析
        if not hasattr(pool, "_network_backend"):   # pragma: no cover - 依赖升级时的保险
            raise RuntimeError("httpcore 版本不兼容：无法把连接钉在核验过的 IP 上，联网工具不可用")
        pool._network_backend = backend


_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME = re.compile(rf"(?:{_LABEL}\.)*{_LABEL}")
_UNSAFE = re.compile(r"[\x00-\x20\x7f\\]")


def canonical_url(url: str) -> tuple[str, str, str, int]:
    """严格解析并重建网址，返回 (规范网址, 协议, 主机, 端口)。范围判定和实际发送都用规范网址：
    同一个字符串在这里、httpx 和读取服务那里解析出的主机必须一致。

    - 空白、控制字符、反斜杠一律拒绝：``urlsplit`` 会悄悄删掉制表符，WHATWG 解析器把反斜杠当路径分隔符，
      ``https://evil.example\\.allowed.example/`` 在这里是一个主机、在读取服务那里是另一个；
    - 带用户名密码的拒绝；主机名只能是合法域名（IDNA 转成 ASCII、小写、去掉末尾的点）或 IP 字面量；
    - 片段丢弃（不会发给服务器），默认端口省略，路径和查询串原样保留。"""
    if _UNSAFE.search(url):
        raise WebError("blocked", "网址里不能有空白、控制字符或反斜杠")
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        raise WebError("bad_request", f"网址写法不对：{url[:200]}") from None
    scheme = u.scheme.lower()
    if u.username is not None or u.password is not None or "@" in (u.netloc or ""):
        raise WebError("blocked", "网址里不能带用户名或密码")
    raw = (u.hostname or "").rstrip(".")
    if not raw:
        raise WebError("bad_request", f"网址缺少主机名：{url[:200]}")
    try:
        host = str(ipaddress.ip_address(raw))
        netloc_host = f"[{host}]" if ":" in host else host
    except ValueError:
        try:
            host = raw.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise WebError("bad_request", f"主机名不合法：{raw[:100]}") from None
        if not _HOSTNAME.fullmatch(host):
            raise WebError("bad_request", f"主机名不合法：{raw[:100]}")
        netloc_host = host
    default = 443 if scheme == "https" else 80
    netloc = netloc_host if port in (None, default) else f"{netloc_host}:{port}"
    return urlunsplit((scheme, netloc, u.path or "/", u.query, "")), scheme, host, port or default


def _host_of(url: str) -> tuple[str, str, int]:
    _, scheme, host, port = canonical_url(url)
    return scheme, host, port


class WebClient:
    """``resolve`` 与 ``transport`` 可替换（测试用假解析器和 ``httpx.MockTransport``）。给了 ``transport`` 时不再钉 IP，
    核验照旧执行：测试断言的是“哪些请求被放出去了”。"""

    def __init__(self, *, timeout: float = 15.0, resolve: Resolver = _getaddrinfo, transport: httpx.AsyncBaseTransport | None = None,
                 user_agent: str = USER_AGENT, backend: httpcore.AsyncNetworkBackend | None = None):
        self.timeout = timeout
        self.resolve = resolve
        self._pinned = _PinnedBackend(backend) if transport is None else None
        # 客户端在进程内跨会话共享：默认的 Cookie 罐会把 A 站下发的 Cookie 带进下一次请求（跨会话追踪、串号），
        # 所以换成一个什么都不收的
        jar = http.cookiejar.CookieJar(policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
        self._client = httpx.AsyncClient(
            transport=transport or _PinnedTransport(self._pinned),   # type: ignore[arg-type]
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)), follow_redirects=False, trust_env=False,
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}, cookies=jar)
        self.calls = 0          # 实际发出的请求数（测试用：确定性回放时必须是 0）

    async def aclose(self) -> None:
        await self._client.aclose()

    async def vet(self, url: str, *, allow_http: Iterable[str] = (), allow_private: Iterable[str] = ()) -> tuple[str, int, list[str]]:
        """协议 + 地址两项检查，返回 (主机, 端口, 核过的 IP)。范围由调用方判定。"""
        scheme, host, port = _host_of(url)
        if scheme not in ("https", "http") or (scheme == "http" and host not in set(allow_http)):
            raise WebError("blocked", f"只允许 https 网址：{url[:200]}")
        private_ok = host in set(allow_private)

        def reaches(ip: IPAddress) -> bool:
            scope = address_scope(ip)
            return scope == "public" or (private_ok and scope in ("private", "loopback"))

        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        try:
            if literal is not None:   # 写成 IP 的网址不解析，同样要核验
                if not reaches(literal):
                    raise EgressDenied(f"{host} 是{_scope_text(literal)}，联网工具不连这类地址")
                ips = [str(literal)]
            else:
                ips = await vet_host(host, port, reaches, self.resolve, "联网工具只连公网地址")
        except EgressDenied as e:
            if literal is None:   # 不说解析到了哪些 IP：否则 web_fetch 就成了查内网 DNS 的工具，结果还进模型上下文
                raise WebError("blocked", f"{host} 解析到的不是公网地址，联网工具只连公网地址") from None
            raise WebError("blocked", str(e)) from None
        except OSError as e:
            raise WebError("transient", f"解析 {host} 失败：{e or type(e).__name__}") from None
        return host, port, ips

    async def request(self, method: str, url: str, *, json: Any = None, headers: dict[str, str] | None = None,
                      in_scope: Callable[[str, str], bool] = lambda host, url: False, redirects: int = 0,
                      max_bytes: int = 2_000_000, truncate: bool = False, accept: Iterable[str] | None = None,
                      allow_http: Iterable[str] = (), allow_private: Iterable[str] = (),
                      timeout: float | None = None) -> Fetched:
        """发一个请求并读完响应。``in_scope(host, url)`` 是调用方的范围判定（后端主机、fetch_scope），每一跳都要过。"""
        hops: list[str] = []
        accept_set = {a.lower() for a in accept} if accept is not None else None
        cur, cur_method, body = url, method, json
        for _ in range(redirects + 1):
            cur = canonical_url(cur)[0]   # 核验、范围判定、发送用同一个字符串
            host, _port, ips = await self.vet(cur, allow_http=allow_http, allow_private=allow_private)
            if not in_scope(host, cur):
                raise WebError("blocked", f"{host} 不在允许的范围内")
            if self._pinned is not None:
                self._pinned.pins[host] = ips
            hops.append(cur)
            self.calls += 1
            try:
                req = self._client.build_request(
                    cur_method, cur, json=body, headers=headers,
                    timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)) if timeout else httpx.USE_CLIENT_DEFAULT)
                resp = await self._client.send(req, stream=True)
            except httpx.TimeoutException as e:
                raise WebError("transient", f"请求 {host} 超时（{type(e).__name__}）") from None
            except httpx.HTTPError as e:
                raise WebError("transient", f"请求 {host} 失败：{type(e).__name__}: {e}") from None
            try:
                if resp.status_code in _REDIRECTS and "location" in resp.headers:
                    if len(hops) > redirects:
                        raise WebError("blocked", f"重定向超过 {redirects} 次")
                    cur = urljoin(cur, resp.headers["location"])
                    if resp.status_code == 303 or (resp.status_code in (301, 302) and cur_method == "POST"):
                        cur_method, body = "GET", None
                    continue
                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if accept_set is not None and resp.status_code < 300 and ctype not in accept_set \
                        and not (ctype.startswith("text/") and "text/*" in accept_set):
                    raise WebError("bad_type", f"不支持的内容类型 {ctype or '（未声明）'}")
                buf = bytearray()
                cut = False
                async for chunk in resp.aiter_bytes():   # 解压后的字节：按它计上限，压缩炸弹在这里被截住
                    buf += chunk
                    if len(buf) > max_bytes:
                        if not truncate:
                            raise WebError("too_large", f"响应超过 {max_bytes} 字节")
                        del buf[max_bytes:]
                        cut = True
                        break
            except httpx.TimeoutException as e:
                raise WebError("transient", f"读取 {host} 的响应超时（{type(e).__name__}）") from None
            except httpx.HTTPError as e:
                raise WebError("transient", f"读取 {host} 的响应失败：{type(e).__name__}: {e}") from None
            finally:
                await resp.aclose()
            return Fetched(resp.status_code, cur, {k.lower(): v for k, v in resp.headers.items()}, bytes(buf), cut, hops)
        raise WebError("blocked", f"重定向超过 {redirects} 次")   # pragma: no cover - 循环里已经判定


def _scope_text(ip: IPAddress) -> str:
    return {"loopback": "本机回环地址", "local": "链路本地、保留或云元数据地址", "private": "内网地址"}.get(address_scope(ip), "公网地址")


__all__ = ["Fetched", "USER_AGENT", "WebClient", "WebError", "canonical_url"]
