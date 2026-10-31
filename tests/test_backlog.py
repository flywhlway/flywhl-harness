"""能力清单（docs/quality-audit/capability-backlog.md）各项的回归测试。

执行型用例的载荷一律是无害哨兵；仓库根目录的 conftest.py 另有系统沙箱与执行绊线兜底。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

import httpx
import pytest

from flywhl_harness.config import HarnessConfig, HookConfig

from .conftest import make_harness, tool_ends
from .test_security import needs_seatbelt

ROOT = Path(__file__).resolve().parent.parent


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765")


# ---------------------------------------------------------------------- 命令 hook 的环境变量
async def test_command_hook_env_is_scrubbed_like_bash_and_mcp(ws, tmp_path, monkeypatch):
    """hook 脚本是第三方代码：密钥类变量与 bash、MCP 子进程一样剥离，确实要用的在配置里写明。"""
    monkeypatch.setenv("FLYWHL_PROBE_TOKEN", "s3cr3t-token")
    monkeypatch.setenv("DATABASE_URL", "postgres://u:pw@db.example/app")
    monkeypatch.setenv("PLAIN_SETTING", "visible")
    monkeypatch.setenv("WEBHOOK_SECRET", "passed-on-purpose")
    out = tmp_path / "hook-env.txt"
    hc = HookConfig(event="pre_tool_use", command=f"env > {out}", pass_env=["WEBHOOK_SECRET"], env={"EXTRA": "1"})
    h = make_harness(ws, [{"tool_calls": [{"name": "write", "args": {"path": "a.txt", "content": "x"}}]}, "好"], hooks=[hc])
    await h.run("写")
    env = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    assert "FLYWHL_PROBE_TOKEN" not in env and "DATABASE_URL" not in env
    assert env["PLAIN_SETTING"] == "visible" and env["EXTRA"] == "1"
    assert env["WEBHOOK_SECRET"] == "passed-on-purpose" and env["FLYWHL_HOOK_EVENT"] == "pre_tool_use"


async def test_settings_lists_hook_env_names_but_never_values(ws, monkeypatch):
    from flywhl_harness.server.app import create_app

    monkeypatch.setenv("WEBHOOK_SECRET", "never-shown")
    hc = HookConfig(event="post_tool_use", command="true", pass_env=["WEBHOOK_SECRET"], env={"MODE": "strict-value"})
    h = make_harness(ws, ["好"], hooks=[hc])
    async with _client(create_app(h, auto_recover=False)) as c:
        r = await c.get("/api/settings")
    assert r.status_code == 200
    assert r.json()["hooks"][0]["env"] == ["MODE", "WEBHOOK_SECRET"]
    assert "never-shown" not in r.text and "strict-value" not in r.text


def test_hook_env_options_load_from_toml(tmp_path):
    (tmp_path / "flywhl.toml").write_text(
        '[[hooks]]\nevent = "pre_tool_use"\ncommand = "python3 guard.py"\npass_env = ["SLACK_TOKEN"]\n'
        '[hooks.env]\nLEVEL = "2"\n', encoding="utf-8")
    cfg = HarnessConfig.load(workspace=str(tmp_path), trust=lambda _req: True)
    assert cfg.hooks[0].pass_env == ["SLACK_TOKEN"] and cfg.hooks[0].env == {"LEVEL": "2"}


def test_trust_prompt_spells_out_child_env():
    """信任确认只列命令不够：env 里的 PATH、BASH_ENV 能换掉实际执行的程序，pass_env 会把密钥交出去。"""
    from flywhl_harness.config import split_untrusted

    _, risky = split_untrusted({
        "hooks": [{"command": "python3 guard.py", "env": {"PATH": "./bin", "API_TOKEN": "t0p-value", "LANG": "C"},
                   "pass_env": ["OPENAI_API_KEY"]}],
        "mcp_servers": {"gh": {"command": "npx", "args": ["srv"], "env": {"BASH_ENV": "x.sh"}}}})
    hook, mcp = risky
    assert "PATH='./bin'（可能改变实际执行的程序" in hook and "OPENAI_API_KEY" in hook
    assert "API_TOKEN=<已隐藏>" in hook and "t0p-value" not in hook   # 值不上屏，名字照样要确认
    assert "LANG='C'" in hook and "LANG='C'（" not in hook            # 只影响输出格式的变量不额外警告
    assert "BASH_ENV='x.sh'（可能改变" in mcp


# ---------------------------------------------------------------------- MCP 注解
ECHO_SERVER = [sys.executable, str(ROOT / "examples" / "mcp_echo_server.py")]


async def test_mcp_server_claiming_read_only_still_needs_approval(ws):
    """示例服务器给 echo 标了 readOnlyHint：没写明采信时照样按外部副作用审批，无人值守时被拒。"""
    h = make_harness(ws, [{"tool_calls": [{"name": "mcp__demo__echo", "args": {"text": "hi"}}]}, "好"], on_ask="deny",
                     mcp_servers={"demo": {"command": ECHO_SERVER[0], "args": ECHO_SERVER[1:]}})
    async with h:
        echo = h.tools.get("mcp__demo__echo")
        assert str(echo.effect) == "external" and echo.ignored_hints == ["readOnlyHint"]
        res = await h.run("回显")
    assert tool_ends(res.events)[0]["status"] == "rejected"   # 进入了审批（策略直接拒绝是 denied）


@pytest.mark.parametrize("conf", [{"trust_annotations": True}, {"read_only_tools": ["ec*"]}])
async def test_mcp_read_only_needs_config(ws, conf):
    h = make_harness(ws, ["好"], mcp_servers={"demo": {"command": ECHO_SERVER[0], "args": ECHO_SERVER[1:], **conf}})
    async with h:
        assert str(h.tools.get("mcp__demo__echo").effect) == "read"
        assert str(h.tools.get("mcp__demo__add_note").effect) == "external"


async def test_settings_show_mcp_annotation_policy(ws):
    from flywhl_harness.server.app import create_app

    h = make_harness(ws, ["好"], mcp_servers={"demo": {"command": ECHO_SERVER[0], "args": ECHO_SERVER[1:]}})
    async with h:
        async with _client(create_app(h, auto_recover=False)) as c:
            m = (await c.get("/api/settings")).json()["mcp"][0]
    assert m == {"name": "demo", "transport": "stdio", "tools": 2, "trust_annotations": False, "read_only": [],
                 "ignored_hints": 1}


@pytest.mark.parametrize("bad", [{"read_only_tools": "safe_*"}, {"read_only_tools": [1]}, {"trust_annotations": "yes"},
                                 {"env": {"A": 1}}, {"args": "srv.js"}, {"read_only_tool": ["x"]}])
def test_mcp_server_config_types_are_strict(bad):
    """类型写错就报错：read_only_tools = "safe_*" 若按字符拆开，"*" 会放行全部工具，确认提示里却显示 'safe_*'。"""
    from flywhl_harness.config import _merge, split_untrusted

    spec = {"command": "node", **bad}
    with pytest.raises(ValueError, match="mcp_servers.x"):
        _merge(HarnessConfig(), {"mcp_servers": {"x": spec}})
    assert "加载时同样会报错" in split_untrusted({"mcp_servers": {"x": spec}})[1][0]


def test_hook_env_types_are_strict():
    with pytest.raises(ValueError, match="hooks.pass_env"):
        HookConfig(command="true", pass_env="OPENAI_API_KEY")
    with pytest.raises(ValueError, match="hooks.env"):
        HookConfig(command="true", env={"LEVEL": 2})


# ---------------------------------------------------------------------- 出站白名单
async def _upstream(body: bytes = b"HELLO"):
    """本机的一个最小 HTTP 服务，充当“白名单里的外部主机”。"""
    async def handle(reader, writer):
        with contextlib.suppress(Exception):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def test_egress_rules_parse_and_match():
    from flywhl_harness.tools.egress import local_ports, parse_rules

    rules = parse_rules(["pypi.org", "*.github.com", "registry.local:5000", "Example.com:*", "localhost:5432"])
    ok = lambda h, p: any(r.matches(h, p) for r in rules)   # noqa: E731
    assert ok("pypi.org", 443) and ok("pypi.org", 80) and not ok("pypi.org", 8080) and not ok("evil-pypi.org", 443)
    assert ok("api.github.com", 443) and not ok("github.com", 443) and not ok("evilgithub.com", 443)
    assert ok("registry.local", 5000) and not ok("registry.local", 443) and ok("example.com", 1234)
    assert local_ports(rules) == ["5432"]
    for bad in ("https://pypi.org", "*", "*.com:99999", "a b", "pypi.org/simple", ""):
        with pytest.raises(ValueError, match="出站白名单"):
            parse_rules([bad])


async def test_egress_proxy_tunnels_listed_hosts_and_refuses_others():
    from flywhl_harness.tools.egress import EgressProxy, parse_rules

    server, port = await _upstream()
    async with server, EgressProxy(parse_rules([f"127.0.0.1:{port}"])) as proxy:
        async def ask(request: bytes, then: bytes = b"") -> bytes:
            r, w = await asyncio.open_connection("127.0.0.1", proxy.port)
            w.write(request)
            if then:
                assert (await r.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
                w.write(then)
            data = await asyncio.wait_for(r.read(), 5)
            w.close()
            return data

        tunnel = await ask(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode(), b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        plain = await ask(f"GET http://127.0.0.1:{port}/ HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
        refused = await ask(b"CONNECT evil.example:443 HTTP/1.1\r\n\r\n")
        smuggled = await ask(b"CONNECT evil.example%2f.127.0.0.1:443 HTTP/1.1\r\n\r\n")
    assert tunnel.endswith(b"HELLO") and plain.endswith(b"HELLO")
    assert refused.startswith(b"HTTP/1.1 403") and "evil.example:443".encode() in refused
    assert smuggled.startswith(b"HTTP/1.1 400")   # 主机名不规范：不判定、不连接
    assert proxy.denied == ["evil.example:443"]


@pytest.mark.parametrize("network", ["deny", "allowlist"])
async def test_network_policy_without_sandbox_refuses_to_run(ws, network):
    """网络限制要靠系统沙箱执行：没开沙箱就拒绝执行，不悄悄联网。"""
    h = make_harness(ws, [{"tool_calls": [{"name": "bash", "args": {"command": "touch ran.txt"}}]}, "好"],
                     permissions__mode="bypass", permissions__sandbox_network=network)
    end = tool_ends((await h.run("跑")).events)[0]
    assert "命令未执行" in end["content"] and not (ws / "ran.txt").exists()


def test_untrusted_project_cannot_open_an_allowlist():
    from flywhl_harness.config import split_untrusted

    _, risky = split_untrusted({"permissions": {"sandbox_network": "allowlist", "network_allow": ["evil.example"]}})
    assert risky == ["permissions.sandbox_network = 'allowlist'", "permissions.network_allow = ['evil.example']"]
    assert split_untrusted({"permissions": {"network_allow": []}})[1] == []


def test_bad_allowlist_entry_fails_at_startup(ws):
    with pytest.raises(ValueError, match="出站白名单"):
        make_harness(ws, ["好"], permissions__sandbox="macos", permissions__sandbox_network="allowlist",
                     permissions__network_allow=["https://pypi.org/simple"])


@needs_seatbelt
async def test_sandbox_allowlist_reaches_only_listed_hosts(ws):
    up, port = await _upstream()
    other, other_port = await _upstream(b"OTHER")
    connect = 'python3 -c "import socket,sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), 2)" {} {} 2>&1 | tail -1'
    cmds = [f"curl -s http://127.0.0.1:{port}/",                                   # 白名单内，经代理转发
            f"curl -s -p http://127.0.0.1:{port}/",                                # 白名单内，经 CONNECT 隧道
            f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{other_port}/",   # 不在白名单：代理回 403
            connect.format("127.0.0.1", other_port),                               # 绕过代理直连本机端口：内核拒绝
            connect.format("192.0.2.1", 80)]                                       # 直连外部地址（TEST-NET-1）：内核拒绝
    h = make_harness(ws, [{"tool_calls": [{"name": "bash", "args": {"command": c}}]} for c in cmds] + ["好"],
                     permissions__mode="bypass", permissions__sandbox="macos", permissions__sandbox_network="allowlist",
                     permissions__network_allow=[f"127.0.0.1:{port}"])
    async with up, other:
        ends = tool_ends((await h.run("连一下")).events)
    out = [e["content"] for e in ends]
    assert out[0].startswith("HELLO") and out[1].startswith("HELLO"), out
    assert out[2].startswith("403") and f"127.0.0.1:{other_port}" in out[2], out[2]
    assert "PermissionError" in out[3] and "PermissionError" in out[4], out


@needs_seatbelt
@pytest.mark.parametrize("network", ["deny", "allowlist"])
async def test_sandbox_network_policies_block_unix_sockets(ws, network):
    """DNS 查询走 mDNSResponder 的 Unix 套接字，数据能编码进域名带出去；docker.sock 能起带网络的容器。"""
    d = tempfile.mkdtemp(prefix="fw", dir="/tmp")   # Unix 套接字路径不能超过 104 字节
    sock = os.path.join(d, "s")
    server = await asyncio.start_unix_server(lambda r, w: w.close(), sock)
    cmd = f'python3 -c "import socket,sys; socket.socket(socket.AF_UNIX).connect(sys.argv[1])" {sock} 2>&1 | tail -1'
    h = make_harness(ws, [{"tool_calls": [{"name": "bash", "args": {"command": cmd}}]}, "好"],
                     permissions__mode="bypass", permissions__sandbox="macos", permissions__sandbox_network=network)
    try:
        async with server:
            out = tool_ends((await h.run("连一下")).events)[0]["content"]
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert "PermissionError" in out, out


def test_cli_net_allow_turns_on_the_allowlist(tmp_path):
    import argparse

    from flywhl_harness.cli import _config

    cfg = _config(argparse.Namespace(config=None, workspace=str(tmp_path), provider=None, model=None, mode=None, yes=False,
                                     trust=False, json=True, sandbox="macos", net_allow=["pypi.org", "*.github.com"]))
    assert cfg.permissions.sandbox_network == "allowlist" and cfg.permissions.network_allow == ["pypi.org", "*.github.com"]


@pytest.mark.parametrize("cmd", ["serve", "chat", "run", "tools"])
def test_cli_parser_builds(cmd, capsys):
    """公共参数与子命令参数不能重名：重名时 argparse 在启动时就抛错，单测 _config 发现不了。"""
    from flywhl_harness.cli import main

    with pytest.raises(SystemExit) as e:
        main([cmd, "--help"])
    assert e.value.code == 0 and "--net-allow" in capsys.readouterr().out
