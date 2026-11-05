"""最小 MCP（Model Context Protocol）客户端：把 MCP 服务器的工具接入 harness。

MCP 与 Function Calling 的关系：Function Calling 是“模型 ↔ harness”的协议（模型说想调用什么）；
MCP 是“harness ↔ 工具服务器”的协议（工具在哪、怎么调）。harness 把 MCP 工具翻译成普通工具交给模型。

传输：stdio，按行分帧的 JSON-RPC 2.0 消息。握手 ``initialize`` → 通知 ``notifications/initialized``，
之后 ``tools/list``（支持分页游标）与 ``tools/call``。

治理要点
--------
- **命名空间**：工具名加前缀 ``mcp__<server>__<tool>``，避免与内置工具冲突，也便于按服务器写权限规则；
- **副作用分级**：annotations 是服务器的自我声明，规范要求客户端把它当作不可信的提示。默认一律按最保守处理：
  外部副作用、输出视为不可信（污点）、不幂等、取消时不直接中断。放宽有两条路，都写在配置里，接入时要经信任确认：
  ``read_only_tools`` 点名按只读处理的工具；``trust_annotations = true`` 采信该服务器的 ``readOnlyHint``（只读）、
  ``idempotentHint``（“结果未知”时允许用同一幂等键重试）与 ``openWorldHint = false``（输出不算污点）；
- **环境隔离**：服务器进程拿不到 harness 进程里的密钥类环境变量，需要的凭证在配置的 ``env`` 里显式给出；
- **系统沙箱**：服务器和 bash 一样跑在沙箱里，读不到凭证文件，有自己的 /tmp 与可写目录（``tools/confine.py``）。网络默认跟随全局，
  ``sandbox_network``、``network_allow`` 可以单独设；``sandbox = "off"`` 只在用户配置里有效；
- **超时即未知**：``tools/call`` 不设客户端超时，由执行器统一计时；超时或取消时向服务器发送
  ``notifications/cancelled``，非只读工具的超时按“结果未知”上报；
- **供应链**：MCP 服务器就是在你机器上运行的代码，接入前要审查来源、锁定版本，并只授予必要的权限。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from fnmatch import fnmatchcase
from typing import Any

from .. import __version__

from ..config import check_mcp_server
from .base import Effect, Tool, ToolContext, ToolResult
from .confine import Confinement, SandboxUnavailable
from .shell import _clean_env

PROTOCOL_VERSION = "2025-06-18"
# 会放宽处理的注解取值：只读、幂等、封闭世界
_LOOSENING = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}


class MCPError(Exception):
    pass


class MCPClient:
    def __init__(self, name: str, command: str, args: list[str] | None = None, env: dict[str, str] | None = None,
                 cwd: str | None = None, timeout: float = 30.0, *, confinement: Confinement | None = None,
                 workspace: str | None = None):
        self.name = name
        self.confinement = confinement or Confinement.resolve()
        self.workspace = workspace or cwd or "."
        self._launch: Any = None        # enter() 的上下文：出站代理活到服务器退出
        self._proxy: Any = None
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.cwd = cwd
        self.timeout = timeout
        self.proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self.server_info: dict[str, Any] = {}

    async def start(self) -> "MCPClient":
        launch = self.confinement.enter([self.command, *self.args], self.workspace, {**_clean_env(True), **self.env},
                                        chdir=self.cwd)
        try:
            run = await launch.__aenter__()
        except SandboxUnavailable as e:
            raise MCPError(f"MCP 服务器 {self.name} 没有启动：{e}") from None
        self._launch, self._proxy = launch, run.proxy
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *run.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=run.env, cwd=self.cwd,
            )
            self._reader = asyncio.create_task(self._read_loop())
            init = await self.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                "clientInfo": {"name": "flywhl-harness", "version": __version__},
            })
            self.server_info = init.get("serverInfo", {})
            await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except BaseException as e:   # 取消也要收拾：服务器和它的出站代理不能留下
            await self.close()
            if isinstance(e, (MCPError, OSError, asyncio.TimeoutError)):   # OSError：命令不存在，或者服务器一启动就退出、写不进 stdin
                raise MCPError(f"MCP 服务器 {self.name} 启动失败：{e or type(e).__name__}{self._start_hint()}") from e
            raise
        return self

    def _start_hint(self) -> str:
        """启动失败多半是沙箱拦的：npx、uvx 第一次运行要下载包，默认的空白名单连不出去。"""
        box = self.confinement
        if box.level == "off":
            return ""
        denied = sorted(set(self._proxy.denied)) if self._proxy else []
        hint = f"。它在系统沙箱里运行（网络策略 {box.network}）"
        if denied:
            hosts = "、".join(denied)
            return f"{hint}，出站白名单拦下了 {hosts}：把主机写进 [mcp_servers.{self.name}] 的 network_allow"
        return f'{hint}，确实需要沙箱外的文件或凭证时，在用户配置的 [mcp_servers.{self.name}] 里写 sandbox = "off"'

    async def _send(self, msg: dict[str, Any]) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def _read_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # 服务器打印到 stdout 的非协议内容：忽略
            mid = msg.get("id")
            if mid is not None and "method" in msg:
                # 服务器发来的请求（ping、sampling……）：必须回复，否则服务器那边会一直等
                reply = {"result": {}} if msg["method"] == "ping" else \
                    {"error": {"code": -32601, "message": f"客户端不支持 {msg['method']}"}}
                with contextlib.suppress(Exception):
                    await self._send({"jsonrpc": "2.0", "id": mid, **reply})
                continue
            if mid is not None and mid in self._pending and ("result" in msg or "error" in msg):
                fut = self._pending.pop(mid)
                if not fut.done():
                    if "error" in msg:
                        fut.set_exception(MCPError(f"{msg['error'].get('code')}: {msg['error'].get('message')}"))
                    else:
                        fut.set_result(msg["result"])
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(MCPError(f"MCP 服务器 {self.name} 已退出"))

    async def request(self, method: str, params: dict[str, Any] | None = None, *,
                      timeout: float | None | bool = True) -> dict[str, Any]:
        """发送请求并等待响应。``timeout=None`` 表示不设客户端超时（由调用方计时与取消）。"""
        self._next_id += 1
        mid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
        try:
            return await asyncio.wait_for(fut, self.timeout if timeout is True else timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            # 告诉服务器“不用做了”（尽力而为：已经发生的副作用不会因此撤销）
            with contextlib.suppress(Exception):
                await self._send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                  "params": {"requestId": mid, "reason": "客户端取消或超时"}})
            raise
        finally:
            self._pending.pop(mid, None)

    async def list_tools(self) -> list[dict[str, Any]]:
        tools, cursor = [], None
        while True:
            res = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(res.get("tools", []))
            cursor = res.get("nextCursor")
            if not cursor:
                return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.request("tools/call", {"name": name, "arguments": arguments}, timeout=None)

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            with contextlib.suppress(Exception):
                self.proc.stdin.close()  # type: ignore[union-attr]
                await asyncio.wait_for(self.proc.wait(), 3)
            if self.proc.returncode is None:
                self.proc.kill()
        if self._reader:
            self._reader.cancel()
        if self._launch is not None:
            launch, self._launch = self._launch, None
            await launch.__aexit__(None, None, None)


class MCPTool(Tool):
    def __init__(self, client: MCPClient, spec: dict[str, Any], *, trust_annotations: bool = False,
                 read_only: bool = False):
        self.client = client
        self.remote_name = spec["name"]
        self.name = f"mcp__{client.name}__{spec['name']}"
        self.description = (spec.get("description") or "")[:2000] + f"（来自 MCP 服务器 {client.name}）"
        self.parameters = spec.get("inputSchema") or {"type": "object", "properties": {}}
        ann = spec.get("annotations") or {}
        # 恶意服务器自称只读就能绕过审批、自称幂等就能被重做：没写明 trust_annotations 时，注解一项都不采信
        hints = ann if trust_annotations else {}
        self.ignored_hints = [] if trust_annotations else [k for k, v in _LOOSENING.items() if ann.get(k) is v]
        read_only = read_only or hints.get("readOnlyHint") is True
        self.effect = Effect.READ if read_only else Effect.EXTERNAL
        self.untrusted_output = hints.get("openWorldHint", True) is not False   # 规范默认值为 true
        self.idempotent = read_only or hints.get("idempotentHint") is True
        self.interruptible = read_only

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        res = await self.client.call_tool(self.remote_name, args)
        parts = []
        for c in res.get("content", []):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            else:
                parts.append(f"[{c.get('type')} 内容，已省略]")
        if res.get("structuredContent") is not None and not parts:
            parts.append(json.dumps(res["structuredContent"], ensure_ascii=False))
        return ToolResult("\n".join(parts) or "(空结果)", is_error=bool(res.get("isError")),
                          details={"mcp_server": self.client.name}, tainted=self.untrusted_output)


async def load_mcp_tools(servers: dict[str, dict[str, Any]], cwd: str | None = None,
                         confinement: Confinement | None = None) -> tuple[list[MCPClient], list[MCPTool]]:
    """按配置启动 MCP 服务器并列出工具。配置示例（flywhl.toml）::

        [mcp_servers.echo]
        command = "python"
        args = ["examples/mcp_echo_server.py"]
        read_only_tools = ["echo"]     # 按只读处理的工具，支持 * 通配
        # trust_annotations = true     # 采信服务器自报的只读、幂等、封闭世界注解
    """
    clients, tools = [], []
    box = confinement or Confinement.resolve()
    try:
        for name, conf in servers.items():
            check_mcp_server(name, conf)   # 代码里直接构造的配置不经过 HarnessConfig.load，这里再校验一次
            c = MCPClient(name, conf["command"], conf.get("args"), conf.get("env"), conf.get("cwd") or cwd, workspace=cwd,
                          confinement=box.scoped("mcp", name, conf))
            clients.append(c)
            await c.start()
            trust = conf.get("trust_annotations", False)
            ro = conf.get("read_only_tools", [])
            for spec in sorted(await c.list_tools(), key=lambda s: s["name"]):  # 排序：保证工具顺序稳定
                tools.append(MCPTool(c, spec, trust_annotations=trust, read_only=any(fnmatchcase(spec["name"], p) for p in ro)))
    except BaseException:   # 已经起来的服务器不能留着：它们的出站代理也跟着
        for started in clients:
            await started.close()
        raise
    return clients, tools
