"""联网测试的公共设施：假解析器、按路由应答的 MockTransport、带联网工具的 Harness。

一个真实请求都不发：传输层是 ``httpx.MockTransport``，解析器是查表；密钥一律用 ``FAKE_KEY`` 这种明显的假值。
"""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from typing import Any, Callable

import httpx

from flywhl_harness import Harness, HarnessConfig, ScriptedProvider, Store
from flywhl_harness.llm.base import Provider
from flywhl_harness.net.client import WebClient
from flywhl_harness.net.search import WebService
from flywhl_harness.tools import default_tools

PUBLIC = "93.184.216.34"
FAKE_KEY = "fake-key-for-tests"
FIXTURES = Path(__file__).parent / "fixtures" / "web"
DS_SEARCH = "https://api.deepseek.com/v1/tools/search"
DS_FETCH = "https://api.deepseek.com/v1/tools/fetch"
BOCHA = "https://api.bochaai.com/v1/web-search"


def fixture(backend: str, name: str) -> Any:
    return json.loads((FIXTURES / backend / f"{name}.json").read_text(encoding="utf-8"))


class Resolver:
    """查表解析：值可以是一个地址、地址列表、返回它们的函数，或要抛出的异常。没列出的主机解析到 ``PUBLIC``。"""

    def __init__(self, table: dict[str, Any] | None = None, default: Any = PUBLIC):
        self.table = dict(table or {})
        self.default = default
        self.calls: list[str] = []

    async def __call__(self, host: str, port: int) -> list[Any]:
        self.calls.append(host)
        v = self.table.get(host, self.default)
        if callable(v):
            v = v()
        if isinstance(v, BaseException):
            raise v
        return [ipaddress.ip_address(x) for x in (v if isinstance(v, list) else [v])]


Responder = Callable[[httpx.Request], httpx.Response] | httpx.Response | list


class Net:
    """按 ``(方法, URL 前缀)`` 路由。应答可以是 Response、函数或 Response 列表（按顺序用，用完重复最后一个）。
    没有路由的请求返回 599，测试里能直接看到漏网的请求。"""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Responder]] = []
        self.requests: list[httpx.Request] = []

    def on(self, method: str, prefix: str, responder: Responder) -> "Net":
        self.routes.append((method.upper(), prefix, responder))
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        for method, prefix, r in self.routes:
            if request.method == method and url.startswith(prefix):
                if isinstance(r, list):
                    r = r.pop(0) if len(r) > 1 else r[0]
                return r(request) if callable(r) else r
        return httpx.Response(599, text=f"测试里没有这条路由：{request.method} {url}")

    def bodies(self, prefix: str = "") -> list[dict]:
        return [json.loads(r.content or b"{}") for r in self.requests if str(r.url).startswith(prefix)]

    def client(self, resolver: Resolver | None = None, **kw: Any) -> WebClient:
        return WebClient(transport=httpx.MockTransport(self.handler), resolve=resolver or Resolver(), **kw)


def ds_search(results: list[dict] | None = None) -> httpx.Response:
    return httpx.Response(200, json={"results": fixture("deepseek", "search")["results"] if results is None else results})


class Sleeps(list):
    async def __call__(self, seconds: float) -> None:
        self.append(seconds)


def web_config(ws: Path, **over: Any) -> HarnessConfig:
    cfg = HarnessConfig.load(workspace=str(ws))
    cfg.permissions.on_ask = over.pop("on_ask", "allow")
    cfg.snapshots = False
    cfg.heartbeat_seconds = 0.2
    cfg.trust_workspace = True
    cfg.web.enabled = True
    cfg.web.default = "search"
    for k, v in over.items():
        target = cfg
        *path, last = k.split("__")
        for p in path:
            target = getattr(target, p)
        setattr(target, last, v)
    return cfg


def make_web_harness(ws: Path, script: list[Any] | Provider, net: Net, *, resolver: Resolver | None = None,
                     env: dict[str, str] | None = None, store: Store | None = None, sleeps: Sleeps | None = None,
                     **over: Any) -> Harness:
    cfg = web_config(ws, **over)
    svc = WebService(cfg.web, env={"DEEPSEEK_API_KEY": FAKE_KEY} if env is None else env, client=net.client(resolver),
                     sleep=sleeps if sleeps is not None else Sleeps())
    provider = script if isinstance(script, Provider) else ScriptedProvider(script)
    return Harness(cfg, provider=provider, model="scripted", store=store or Store(":memory:"),
                   tools=default_tools(cfg, cfg.data_path(), web=svc))


def search_call(query: str = "httpx latest version", **args: Any) -> dict:
    return {"tool_calls": [{"name": "web_search", "args": {"query": query, **args}}]}


def fetch_call(url: str, **args: Any) -> dict:
    return {"tool_calls": [{"name": "web_fetch", "args": {"url": url, **args}}]}


def tool_results(events: list, name: str | None = None) -> list[dict]:
    from flywhl_harness.events import E

    return [e.data for e in events if e.type == E.TOOL_END and (name is None or e.data.get("name") == name)]
