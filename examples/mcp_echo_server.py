"""一个只用标准库实现的最小 MCP 服务器（stdio），用于演示与测试 MCP 接入。

提供两个工具：
- ``echo``（只读）：原样返回文本；
- ``add_note``（有副作用）：把笔记追加到 notes.txt。

在 flywhl.toml 中接入::

    [mcp_servers.demo]
    command = "python"
    args = ["examples/mcp_echo_server.py"]
"""

import json
import sys
from pathlib import Path

TOOLS = [
    {
        "name": "echo",
        "description": "原样返回输入的文本",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "add_note",
        "description": "把一条笔记追加到 notes.txt",
        "inputSchema": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]},
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
]


def reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    msg.update({"error": error} if error else {"result": result})
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        continue
    method, mid, params = req.get("method"), req.get("id"), req.get("params") or {}
    if mid is None:  # 通知（如 notifications/initialized）无需回复
        continue
    if method == "initialize":
        reply(mid, {"protocolVersion": params.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
                    "serverInfo": {"name": "flywhl-echo", "version": "0.1.0"}})
    elif method == "tools/list":
        reply(mid, {"tools": TOOLS})
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name == "echo":
            reply(mid, {"content": [{"type": "text", "text": args.get("text", "")}], "isError": False})
        elif name == "add_note":
            with Path("notes.txt").open("a", encoding="utf-8") as f:
                f.write(args.get("note", "") + "\n")
            reply(mid, {"content": [{"type": "text", "text": "已记录"}], "isError": False})
        else:
            reply(mid, {"content": [{"type": "text", "text": f"未知工具 {name}"}], "isError": True})
    else:
        reply(mid, error={"code": -32601, "message": f"Method not found: {method}"})
