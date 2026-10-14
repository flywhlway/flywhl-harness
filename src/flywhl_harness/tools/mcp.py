"""最小 MCP（Model Context Protocol）客户端：把 MCP 服务器的工具接入 harness。

MCP 与 Function Calling 的关系：Function Calling 是“模型 ↔ harness”的协议（模型说想调用什么）；
MCP 是“harness ↔ 工具服务器”的协议（工具在哪、怎么调）。harness 把 MCP 工具翻译成普通工具交给模型。

传输：stdio，按行分帧的 JSON-RPC 2.0 消息。握手 ``initialize`` → 通知 ``notifications/initialized``，
之后 ``tools/list``（支持分页游标）与 ``tools/call``。

治理要点
--------
- **命名空间**：工具名加前缀 ``mcp__<server>__<tool>``，避免与内置工具冲突，也便于按服务器写权限规则；
- **副作用分级**：依据 MCP 的 annotations——``readOnlyHint`` 为只读；否则按外部副作用处理（保守）；
  ``openWorldHint`` 按规范 **默认为 true**：除非服务器明确声明 false，输出都视为不可信（污点）；
  ``idempotentHint`` 为 true 的写工具在“结果未知”时允许用同一幂等键重试；
- **环境隔离**：服务器进程拿不到 harness 进程里的密钥类环境变量，需要的凭证在配置的 ``env`` 里显式给出；
- **超时即未知**：``tools/call`` 不设客户端超时，由执行器统一计时；超时或取消时向服务器发送
  ``notifications/cancelled``，非只读工具的超时按“结果未知”上报；
- **供应链**：MCP 服务器就是在你机器上运行的代码，接入前要审查来源、锁定版本，并只授予必要的权限。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from .base import Effect, Tool, ToolContext, ToolResult
from .shell import _clean_env

PROTOCOL_VERSION = "2025-06-18"


class MCPError(Exception):
    pass


class MCPClient:
    def __init__(self, name: str, command: str, args: list[str] | None = None, env: dict[str, str] | None = None,
                 cwd: str | None = None, timeout: float = 30.0):
        self.name = name
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
        self.proc = await asyncio.create_subprocess_exec(
            self.command, *self.args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env={**_clean_env(True), **self.env}, cwd=self.cwd,
        )
        self._reader = asyncio.create_task(self._read_loop())
        init = await self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "flywhl-harness", "version": "0.1.0"},
        })
        self.server_info = init.get("serverInfo", {})
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

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


class MCPTool(Tool):
    def __init__(self, client: MCPClient, spec: dict[str, Any]):
        self.client = client
        self.remote_name = spec["name"]
        self.name = f"mcp__{client.name}__{spec['name']}"
        self.description = (spec.get("description") or "")[:2000] + f"（来自 MCP 服务器 {client.name}）"
        self.parameters = spec.get("inputSchema") or {"type": "object", "properties": {}}
        ann = spec.get("annotations") or {}
        read_only = bool(ann.get("readOnlyHint", False))
        self.effect = Effect.READ if read_only else Effect.EXTERNAL
        self.untrusted_output = bool(ann.get("openWorldHint", True))   # 规范默认值为 true
        self.idempotent = read_only or bool(ann.get("idempotentHint", False))
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


async def load_mcp_tools(servers: dict[str, dict[str, Any]], cwd: str | None = None) -> tuple[list[MCPClient], list[MCPTool]]:
    """按配置启动 MCP 服务器并列出工具。配置示例（flywhl.toml）::

        [mcp_servers.echo]
        command = "python"
        args = ["examples/mcp_echo_server.py"]
    """
    clients, tools = [], []
    for name, conf in servers.items():
        c = MCPClient(name, conf["command"], conf.get("args"), conf.get("env"), conf.get("cwd") or cwd)
        await c.start()
        clients.append(c)
        for spec in sorted(await c.list_tools(), key=lambda s: s["name"]):  # 排序：保证工具顺序稳定
            tools.append(MCPTool(c, spec))
    return clients, tools
