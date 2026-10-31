"""出站白名单：沙箱里的命令只能连本机的一个代理，代理按主机名放行。

Seatbelt 的网络过滤只认 ``localhost`` 或 ``*`` 加端口，写不了主机名，也写不了具体 IP。所以分两层：

1. **内核层**（``tools/sandbox.py``）：对外的 IP 连接、本机 Unix 套接字一律拒绝，只放行到本机代理端口的连接。
   Unix 套接字也要拒绝：DNS 查询走 mDNSResponder 的套接字，数据可以编码进域名查询带出去；docker.sock 能起一个带网络的容器；
2. **代理层**（本模块）：HTTPS 走 ``CONNECT 主机:端口`` 隧道，代理只看主机名和端口，不解密；纯 HTTP 按绝对 URI 转发，
   Host 头换成判定过的主机。主机不在白名单里就回 403，正文写明主机与配置项，命令输出里看得到原因。
   主机名由代理解析，沙箱里不需要 DNS。解析出的地址还要再核一遍：代理在沙箱外，能连本机端口与内网，
   被放行的域名解析到 127.0.0.1 或云元数据地址（169.254.169.254），就等于绕过了内核层。所以回环、链路本地这类地址
   只有显式的本机规则（``localhost:5432``）才放行，私有地址不接受通配规则；连接用的就是核过的 IP，中间不再解析（防 DNS 重绑定）。

每条命令一个代理实例，监听 127.0.0.1 的临时端口，命令结束就关掉：被拒的主机准确归到这条命令上，命令留在后台的进程也随之断网。

白名单写法（``permissions.network_allow``）：

- ``pypi.org``：只匹配这个主机，端口 80、443；
- ``*.githubusercontent.com``：匹配所有子域名，不含它本身；
- ``registry.local:5000``、``example.com:*``：限定端口或放开端口；
- ``localhost:5432``：本机端口，除了经代理，还在内核层直接放行（数据库这类不支持 HTTP 代理的客户端要用）。

不走代理的客户端（SSH、``git://``、不读 ``HTTPS_PROXY`` 的程序）连不出去：失败是预期的，不会绕过白名单。
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import urlsplit

DEFAULT_PORTS = frozenset({80, 443})
_LABEL = r"[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?"
_HOST = re.compile(rf"{_LABEL}(\.{_LABEL})*")
_RULE = re.compile(rf"(\*\.)?{_HOST.pattern}(:(\d{{1,5}}|\*))?")
_LOCAL = ("localhost", "127.0.0.1")
_PROXY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
_REASONS = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}
# 基准测试网段。TUN 模式的代理软件（fake-ip）把所有域名解析到这里，它不是内网，按公网处理
_FAKE_IP = ipaddress.ip_network("198.18.0.0/15")
_SCOPES = {"loopback": "本机回环地址", "local": "链路本地或保留地址", "private": "内网地址"}
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class EgressDenied(Exception):
    """主机名在白名单里，解析出的地址却不放行。不继承 OSError：和“连不上”是两回事。"""


@dataclass(frozen=True)
class AllowRule:
    host: str                        # 小写；子域名通配存成 ".example.com"
    ports: frozenset[int] | None     # None = 任意端口

    def matches(self, host: str, port: int) -> bool:
        ok = host.endswith(self.host) if self.host.startswith(".") else host == self.host
        return ok and (self.ports is None or port in self.ports)

    def reaches(self, ip: IPAddress) -> bool:
        """这条规则放行的主机解析到 ``ip`` 时能不能连：公网都行；内网只认写全名的规则；回环只认本机规则。"""
        scope = _scope(ip)
        return (scope == "public" or str(ip) == self.host or (scope == "private" and not self.host.startswith("."))
                or (scope == "loopback" and self.host in _LOCAL))

    def __str__(self) -> str:
        port = "" if self.ports == DEFAULT_PORTS else ":*" if self.ports is None else f":{min(self.ports)}"
        return ("*" if self.host.startswith(".") else "") + self.host + port


def _scope(ip: IPAddress) -> str:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:   # ::ffff:127.0.0.1 也是回环
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return "local"
    return "private" if ip.is_private and ip not in _FAKE_IP else "public"


async def _getaddrinfo(host: str, port: int) -> list[IPAddress]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [ipaddress.ip_address(str(i[4][0]).split("%")[0]) for i in infos]


def parse_rules(entries: Iterable[str]) -> list[AllowRule]:
    """解析白名单，写法不对抛 ``ValueError``：宁可启动时报错，也不要悄悄放宽或悄悄失效。"""
    rules = []
    for entry in entries:
        s = entry.strip().lower() if isinstance(entry, str) else ""
        host, _, port = s.partition(":")
        if not _RULE.fullmatch(s) or (port not in ("", "*") and not 0 < int(port) < 65536):
            raise ValueError(f"出站白名单条目写法不对：{entry!r}（示例：pypi.org、*.github.com、registry.local:5000）")
        ports = DEFAULT_PORTS if not port else None if port == "*" else frozenset({int(port)})
        rules.append(AllowRule(host[1:] if host.startswith("*.") else host, ports))
    return rules


def local_ports(rules: Iterable[AllowRule]) -> list[str]:
    """白名单里的本机端口，内核层直接放行；``*`` 表示本机所有端口。"""
    out = set()
    for r in rules:
        if r.host in _LOCAL:
            out |= {"*"} if r.ports is None else {str(p) for p in r.ports}
    return sorted(out)


def _target(method: str, target: str) -> tuple[str, int, str]:
    """请求行里的目标 → (主机, 端口, 转发用的路径)。主机名只接受规范写法：判定与连接用的必须是同一个名字。"""
    if method == "CONNECT":
        u = urlsplit("//" + target)
        port, path = u.port, ""
    else:
        u = urlsplit(target)
        if u.scheme != "http":
            raise ValueError("只转发 http:// 的绝对 URI；HTTPS 请用 CONNECT")
        port, path = u.port or 80, (u.path or "/") + (f"?{u.query}" if u.query else "")
    host = (u.hostname or "").rstrip(".")
    if not _HOST.fullmatch(host) or port is None:
        raise ValueError(f"无法识别的目标 {target!r}")
    return host, port, path


class EgressProxy:
    """每条命令一个：只监听 127.0.0.1 的临时端口，``async with`` 退出时关闭并断开所有连接。"""

    def __init__(self, rules: list[AllowRule], *, connect_timeout: float = 30.0,
                 resolve: Callable[[str, int], Awaitable[list[IPAddress]]] = _getaddrinfo):
        self.rules = rules
        self.connect_timeout = connect_timeout
        self.resolve = resolve
        self.denied: list[str] = []
        self.port = 0
        self._server: asyncio.Server | None = None
        self._conns: set[asyncio.Task[Any]] = set()

    async def __aenter__(self) -> "EgressProxy":
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        for t in list(self._conns):
            t.cancel()
        await asyncio.gather(*self._conns, return_exceptions=True)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._server.wait_closed(), 2)

    def env(self) -> dict[str, str]:
        """命令的代理环境变量。NO_PROXY 清空：不走代理的连接反正会被内核拒绝，不如让工具都走代理、拿到明确的 403。"""
        url = f"http://127.0.0.1:{self.port}"
        return {**dict.fromkeys(_PROXY_ENV, url), "NO_PROXY": "", "no_proxy": "", "NODE_USE_ENV_PROXY": "1"}

    def allowed(self, host: str, port: int) -> bool:
        return any(r.matches(host, port) for r in self.rules)

    async def vet(self, host: str, port: int) -> list[str]:
        """主机名过了白名单，再核对解析出的地址，按顺序返回可以连的 IP；一个都没有时抛 ``EgressDenied``。"""
        rules = [r for r in self.rules if r.matches(host, port)]
        addrs = await self.resolve(host, port)
        ok = [str(ip) for ip in addrs if any(r.reaches(ip) for r in rules)]
        if ok:
            return list(dict.fromkeys(ok))
        seen = "、".join(dict.fromkeys(f"{ip}（{_SCOPES[_scope(ip)]}）" for ip in addrs)) or "空"
        raise EgressDenied(f"{host} 解析到 {seen}，白名单规则 {'、'.join(map(str, rules))} 不放行这类地址"
                           "（回环地址要写 localhost:端口，内网地址要写主机全名）")

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._conns.add(task)
        upstream: asyncio.StreamWriter | None = None
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
            line, _, rest = head.partition(b"\r\n")
            try:
                method, target, version = line.decode("latin-1").split(" ", 2)
                host, port, path = _target(method.upper(), target)
            except ValueError as e:
                return await _reply(writer, 400, f"代理无法处理这个请求：{e}")
            if not self.allowed(host, port):
                self.denied.append(f"{host}:{port}")
                return await _reply(writer, 403, f"出站白名单不包含 {host}:{port}（见 permissions.network_allow）")
            try:
                ips = await asyncio.wait_for(self.vet(host, port), self.connect_timeout)
            except EgressDenied as e:
                self.denied.append(f"{host}:{port}")
                return await _reply(writer, 403, str(e))
            except (OSError, asyncio.TimeoutError) as e:
                return await _reply(writer, 502, f"解析 {host} 失败：{e or type(e).__name__}")
            error: BaseException | None = None
            for ip in ips:   # 连接用的是核过的 IP，不再按名字解析一次
                try:
                    ur, upstream = await asyncio.wait_for(asyncio.open_connection(ip, port), self.connect_timeout)
                    break
                except (OSError, asyncio.TimeoutError) as e:
                    error = e
            else:
                return await _reply(writer, 502, f"连接 {host}:{port} 失败：{error or '没有可用地址'}")
            if method.upper() == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:   # 每个连接只转发一个请求：同一连接上的下一个请求可能换了主机，要重新判定
                upstream.write(f"{method} {path} {version}\r\nHost: {host if port == 80 else f'{host}:{port}'}\r\n".encode("latin-1")
                               + b"".join(h + b"\r\n" for h in _forward_headers(rest)) + b"Connection: close\r\n\r\n")
            await asyncio.gather(_pipe(reader, upstream), _pipe(ur, writer))
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
            pass
        finally:
            for w in (upstream, writer):
                if w is not None:
                    w.close()
            self._conns.discard(task)


def _forward_headers(raw: bytes) -> list[bytes]:
    """转发给上游的请求头：Host 换成判定过的主机（否则同一台服务器上可以换站点），去掉代理专用头与 Connection；
    折行续写的旧式头一律丢掉，免得续到别的头后面。"""
    out = []
    for line in raw.split(b"\r\n"):
        name = line.split(b":", 1)[0].strip().lower()
        if line and line[:1] not in (b" ", b"\t") and b":" in line and name not in (b"host", b"connection") \
                and not name.startswith(b"proxy-"):
            out.append(line)
    return out


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    with contextlib.suppress(OSError):
        while data := await src.read(65536):
            dst.write(data)
            await dst.drain()
    with contextlib.suppress(OSError, RuntimeError):
        if dst.can_write_eof():
            dst.write_eof()   # 半关闭：TLS 的 close_notify 之后对端还可能有数据


async def _reply(writer: asyncio.StreamWriter, code: int, text: str) -> None:
    body = (text + "\n").encode("utf-8")
    writer.write(f"HTTP/1.1 {code} {_REASONS[code]}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                 f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii") + body)
    with contextlib.suppress(OSError):
        await writer.drain()


__all__ = ["AllowRule", "EgressDenied", "EgressProxy", "local_ports", "parse_rules"]
