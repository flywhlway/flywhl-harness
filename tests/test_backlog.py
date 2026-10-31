"""能力清单（docs/quality-audit/capability-backlog.md）各项的回归测试。

执行型用例的载荷一律是无害哨兵；仓库根目录的 conftest.py 另有系统沙箱与执行绊线兜底。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from flywhl_harness.config import HarnessConfig, HookConfig
from flywhl_harness.policy.bash_analyzer import analyze

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
async def _upstream(body: bytes = b"HELLO", *, echo: bool = False):
    """本机的一个最小 HTTP 服务，充当“白名单里的外部主机”。``echo`` 时把收到的请求头原样返回。"""
    async def handle(reader, writer):
        with contextlib.suppress(Exception):
            head = await reader.readuntil(b"\r\n\r\n")
            out = head if echo else body
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(out), out))
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
    # 放行的套接字可能连着 docker.sock：仓库里的配置不能自己打开
    assert split_untrusted({"permissions": {"unix_sockets": ["/var/run/docker.sock"]}})[1] == [
        "permissions.unix_sockets = ['/var/run/docker.sock']"]


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
@pytest.mark.parametrize("network", ["allow", "deny", "allowlist"])
async def test_sandbox_blocks_unix_sockets_unless_listed(ws, network, monkeypatch):
    """Unix 套接字连着本机守护进程：docker.sock 能起挂载家目录的容器，tmux 能替命令在沙箱外执行。任何网络策略下都拒绝，
    unix_sockets 列出的才放行（本机数据库这类）。deny / allowlist 下连 DNS 也不放：查询能把数据编码进域名带出去。"""
    from flywhl_harness.tools import sandbox

    # 放行的套接字不能在沙箱可写的目录里；测试只能在临时目录里建套接字，就把 /tmp 从可写目录里拿掉
    monkeypatch.setattr(sandbox, "default_writable", lambda: [os.path.realpath(tempfile.gettempdir())])
    d = tempfile.mkdtemp(prefix="fw", dir="/tmp")   # Unix 套接字路径不能超过 104 字节
    hidden, listed, link = os.path.join(d, "h"), os.path.join(d, "l"), os.path.join(d, "k")
    os.symlink(hidden, link)   # 放行位置上是指向别处的符号链接：连接按真实路径判定，不放行
    probe = 'python3 -c "import socket,sys; socket.socket(socket.AF_UNIX).connect(sys.argv[1]); print(\'CONNECTED\')" {} 2>&1 | tail -1'
    h = make_harness(ws, [{"tool_calls": [{"name": "bash", "args": {"command": probe.format(p)}}]} for p in (hidden, listed, link)]
                     + ["好"], permissions__mode="bypass", permissions__sandbox="macos", permissions__sandbox_network=network,
                     permissions__unix_sockets=[listed, link])
    servers = [await asyncio.start_unix_server(lambda r, w: w.close(), p) for p in (hidden, listed)]
    try:
        async with servers[0], servers[1]:
            out = [e["content"] for e in tool_ends((await h.run("连一下")).events)]
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert "PermissionError" in out[0] and "CONNECTED" in out[1] and "PermissionError" in out[2], out


def test_unix_sockets_are_pinned_at_startup(tmp_path):
    """放行的套接字启动时定下真实位置、之后不再解析：沙箱能写的目录里，它可能被换成指向 docker.sock 的符号链接。
    目录部分解析（/tmp → /private/tmp），最后一级保留原样。"""
    from flywhl_harness.tools.sandbox import pin_sockets

    (tmp_path / "real").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "real")
    (tmp_path / "real" / "s").symlink_to("/var/run/docker.sock")
    [(want, real)] = pin_sockets([str(tmp_path / "alias" / "s")])
    assert want == str(tmp_path / "alias" / "s") and real == os.path.join(os.path.realpath(tmp_path / "real"), "s")


def test_listed_sockets_must_not_sit_in_writable_dirs(tmp_path):
    """放行的套接字在沙箱可写的目录里时拒绝执行：命令能把那个位置换成 docker.sock 的硬链接（同一个 inode，路径照样对得上），
    或者把上层目录整个换掉。目录部分按真实路径比（/tmp 与 /private/tmp）。"""
    from flywhl_harness.tools.sandbox import seatbelt_profile

    for sock, writable in ((str(tmp_path / "ws" / "s"), []), ("/tmp/.s.PGSQL.5432", ["/tmp"])):
        with pytest.raises(RuntimeError, match="沙箱可写的目录"):
            seatbelt_profile(tmp_path / "ws", writable=writable, env={}, unix_sockets=[sock])
    assert "/tmp/.s.PGSQL.5432" in seatbelt_profile(tmp_path / "ws", writable=[], env={}, unix_sockets=["/tmp/.s.PGSQL.5432"])


def test_seatbelt_profile_closes_local_ipc():
    """钥匙串、剪贴板、LaunchServices / Apple 事件（open、osascript）、launchd 任务：任何网络策略下都拒绝。
    实测见文档“本机 IPC”；放行写在拒绝之后，否则不生效。"""
    from flywhl_harness.tools.sandbox import seatbelt_profile

    allow = seatbelt_profile("/ws", writable=[], env={}, unix_sockets=["/tmp/.s.PGSQL.5432"])
    deny = seatbelt_profile("/ws", writable=[], env={}, network="deny")
    for p in (allow, deny):
        assert all(s in p for s in ('"com.apple.SecurityServer"', '"com.apple.securityd.xpc"', '"com.apple.pasteboard.1"',
                                    "coreservices", "(deny job-creation)", "(deny network-outbound (remote unix-socket))"))
    assert "mDNSResponder" in allow and "mDNSResponder" not in deny
    assert allow.index('"/tmp/.s.PGSQL.5432"') > allow.index("(deny network-outbound (remote unix-socket))")


@needs_seatbelt
async def test_sandbox_blocks_keychain(ws):
    """钥匙串是 macOS 的凭证库，git credential-osxkeychain、security 命令都经 SecurityServer 取口令。
    这里只查系统钥匙串的锁定设置，不碰任何口令；不依赖 HOME（测试里的 HOME 是空的临时目录）。"""
    keychain = "/Library/Keychains/System.keychain"
    if subprocess.run(["security", "show-keychain-info", keychain], capture_output=True).returncode != 0:
        pytest.skip("本机查不了系统钥匙串的设置")
    # 路径在运行时才拼出来：字面的 Library/Keychains、直接赋值的变量在策略层就被认出来拒了，走不到沙箱
    cmd = "K=$(printf /Library/Key); security show-keychain-info ${K}chains/System.keychain; echo EXIT=$?"
    h = make_harness(ws, [{"tool_calls": [{"name": "bash", "args": {"command": cmd}}]}, "好"],
                     permissions__mode="bypass", permissions__sandbox="macos")
    out = tool_ends((await h.run("查一下")).events)[0]["content"]
    assert "EXIT=0" not in out and "EXIT=" in out, out


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


async def test_egress_proxy_rewrites_host_header():
    """纯 HTTP 转发时 Host 头换成判定过的主机：否则连的是放行的服务器，请求的却是同一台机器上的别的站点。"""
    from flywhl_harness.tools.egress import EgressProxy, parse_rules

    server, port = await _upstream(echo=True)
    async with server, EgressProxy(parse_rules([f"127.0.0.1:{port}"])) as proxy:
        r, w = await asyncio.open_connection("127.0.0.1", proxy.port)
        w.write(f"GET http://127.0.0.1:{port}/x HTTP/1.1\r\nHost: evil.example\r\nProxy-Authorization: x\r\n"
                "X-Ok: 1\r\n folded: evil\r\n\r\n".encode())
        seen = (await asyncio.wait_for(r.read(), 5)).split(b"\r\n\r\n", 1)[1]
        w.close()
    assert f"Host: 127.0.0.1:{port}".encode() in seen and b"evil" not in seen and b"Proxy-" not in seen
    assert seen.startswith(b"GET /x HTTP/1.1") and b"X-Ok: 1" in seen


async def test_egress_proxy_checks_resolved_addresses():
    """名字过了白名单，解析出的地址还要再核：代理在沙箱外，解析到本机、元数据地址就等于绕过了内核层。"""
    import ipaddress

    from flywhl_harness.tools.egress import EgressDenied, EgressProxy, parse_rules

    table = {"api.ok.test": ["127.0.0.1"], "meta.ok.test": ["169.254.169.254"], "intra.ok.test": ["10.0.0.5"],
             "mapped.ok.test": ["::ffff:127.0.0.1"], "cdn.ok.test": ["198.18.0.7"], "db.corp": ["10.0.0.6"],
             "localhost": ["::1", "127.0.0.1"], "cgnat.ok.test": ["100.64.1.1"], "nat64.ok.test": ["64:ff9b::a9fe:a9fe"],
             "aliyun-meta.corp": ["100.100.100.200"], "aws-meta6.corp": ["fd00:ec2::254"], "tailnet.corp": ["100.64.1.2"]}

    async def resolve(host, port):
        return [ipaddress.ip_address(a) for a in table[host]]

    proxy = EgressProxy(parse_rules(["*.ok.test", "db.corp", "localhost:5432", "aliyun-meta.corp", "aws-meta6.corp",
                                     "tailnet.corp"]), resolve=resolve)
    for host in ("api.ok.test", "meta.ok.test", "intra.ok.test", "mapped.ok.test", "cgnat.ok.test", "nat64.ok.test",
                 "aliyun-meta.corp", "aws-meta6.corp"):   # 云元数据地址写全名也不放：它们落在 CGNAT、内网段里
        with pytest.raises(EgressDenied, match=host):
            await proxy.vet(host, 443)
    assert await proxy.vet("cdn.ok.test", 443) == ["198.18.0.7"]    # TUN 代理的 fake-ip 网段按公网处理
    assert await proxy.vet("db.corp", 443) == ["10.0.0.6"]          # 写全名的规则可以连内网
    assert await proxy.vet("tailnet.corp", 443) == ["100.64.1.2"]   # CGNAT 段（Tailscale 等）同样按内网
    assert await proxy.vet("localhost", 5432) == ["::1", "127.0.0.1"]


@needs_seatbelt
async def test_sandbox_allowlist_reaps_background_processes(ws):
    """代理随命令关闭，沙箱放行的端口号之后可能被别的服务占用：命令留在后台的进程要一并结束。"""
    h = make_harness(ws, [{"tool_calls": [{"name": "bash", "args": {"command": "sleep 60 >/dev/null 2>&1 & echo $! > bg.pid"}}]}, "好"],
                     permissions__mode="bypass", permissions__sandbox="macos", permissions__sandbox_network="allowlist")
    await h.run("后台跑")
    pid = int((ws / "bg.pid").read_text())
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        os.kill(pid, 9)
        pytest.fail("后台进程在命令结束后仍在运行")


# ---------------------------------------------------------------------- Linux 沙箱（bubblewrap）
def _bwrap_usable() -> bool:
    if not sys.platform.startswith("linux") or not shutil.which("bwrap"):
        return False
    try:
        return subprocess.run(["bwrap", "--ro-bind", "/", "/", "true"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def needs_bwrap(fn):
    for mark in (pytest.mark.os_sandbox, pytest.mark.skipif(not _bwrap_usable(), reason="需要可用的 bubblewrap")):
        fn = mark(fn)
    return fn


@pytest.mark.parametrize("network", ["allow", "allowlist"])
async def test_linux_sandbox_fails_closed(ws, network):
    """没有 bubblewrap（macOS、精简容器），或要求了 Linux 上还没实现的出站白名单：拒绝执行，不退回沙箱外。"""
    if network == "allow" and _bwrap_usable():
        pytest.skip("本机有可用的 bubblewrap")
    h = make_harness(ws, [{"tool_calls": [{"name": "bash", "args": {"command": "touch ran.txt"}}]}, "好"],
                     permissions__mode="bypass", permissions__sandbox="linux", permissions__sandbox_network=network)
    end = tool_ends((await h.run("跑")).events)[0]
    assert "命令未执行" in end["content"] and not (ws / "ran.txt").exists()


@needs_bwrap
async def test_linux_sandbox_hides_credentials_and_guards_harness_state(ws):
    home = Path(os.environ["HOME"])
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("CANARY-SSH\n")
    (home / ".netrc").write_text("CANARY-NETRC\n")
    # 测试里 HOME 在 /tmp 下，会被私有 /tmp 整个盖住；遮蔽本身要靠工作区里各层的凭证来核对
    (ws / "sub").mkdir()
    (ws / "sub" / ".netrc").write_text("CANARY-WS-NETRC\n")
    (ws / "deploy" / ".aws").mkdir(parents=True)
    (ws / "deploy" / ".aws" / "credentials").write_text("CANARY-WS-AWS\n")
    (ws / ".flywhl").mkdir(exist_ok=True)
    # 路径与文件名都在运行时才拼出来：命令分析认不出（字面的 ~/.ssh、直接赋值的变量在策略层就被拒了，走不到沙箱），
    # 只能靠沙箱兜住
    cmds = ["D=$(echo $HOME/.ss); K=$(echo id_ed); N=$(echo $HOME/.ne); W=$(echo sub/.ne); A=$(echo deploy/.aw); "
            "cat ${D}h/${K}25519 ${N}trc ${W}trc ${A}s/cred* 2>&1; echo END",
            "echo hi > inside.txt && cat inside.txt",
            "touch .flywhl/planted 2>/dev/null && echo WROTE || echo READONLY"]
    h = make_harness(ws, [{"tool_calls": [{"name": "bash", "args": {"command": c}}]} for c in cmds] + ["好"],
                     permissions__mode="bypass", permissions__sandbox="linux")
    out = [e["content"] for e in tool_ends((await h.run("看看")).events)]
    assert "CANARY" not in out[0] and "END" in out[0], out[0]
    assert out[1].startswith("hi") and out[2].startswith("READONLY"), out
    assert not (ws / ".flywhl" / "planted").exists()


def test_workspace_credentials_walks_every_level(tmp_path, monkeypatch):
    """Linux 档只能按路径遮蔽：工作区子目录里的凭证也要找出来（目录本身是凭证位置的整个算上）；依赖目录跳过；
    条目多到数不完时拒绝执行，不留漏网的。"""
    from flywhl_harness.tools import sandbox

    for rel in ("deploy/.aws/credentials", "keys/id_rsa_prod", "sub/dir/.netrc", ".ssh/config", "node_modules/pkg/.netrc",
                "src/app.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x")
    found = {os.path.relpath(p, tmp_path) for p in sandbox.workspace_credentials(str(tmp_path))}
    assert found == {"deploy/.aws", "keys/id_rsa_prod", "sub/dir/.netrc", ".ssh"}
    monkeypatch.setattr(sandbox, "_WALK_LIMIT", 3)
    with pytest.raises(RuntimeError, match="没法逐个确认"):
        sandbox.workspace_credentials(str(tmp_path))


@needs_bwrap
async def test_linux_sandbox_hides_host_sockets_but_keeps_session_tmp(ws):
    """宿主 /tmp 里的 tmux、ssh-agent、X11 套接字，只读挂载也挡不住连接：沙箱里的 /tmp 换成本会话私有的目录，
    命令之间照样能用它传文件；unix_sockets 列出的套接字挂回来。"""
    d = tempfile.mkdtemp(prefix="fw", dir="/tmp")
    hidden, listed = os.path.join(d, "h"), os.path.join(d, "l")
    probe = 'python3 -c "import socket,sys; socket.socket(socket.AF_UNIX).connect(sys.argv[1]); print(\'CONNECTED\')" {} 2>&1 | tail -1'
    cmds = [probe.format(hidden), probe.format(listed), "echo kept > /tmp/keep.txt", "cat /tmp/keep.txt"]
    h = make_harness(ws, [{"tool_calls": [{"name": "bash", "args": {"command": c}}]} for c in cmds] + ["好"],
                     permissions__mode="bypass", permissions__sandbox="linux", permissions__unix_sockets=[listed])
    servers = [await asyncio.start_unix_server(lambda r, w: w.close(), p) for p in (hidden, listed)]
    try:
        async with servers[0], servers[1]:
            out = [e["content"] for e in tool_ends((await h.run("连一下")).events)]
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert "CONNECTED" not in out[0] and "CONNECTED" in out[1], out
    assert out[3].startswith("kept") and not os.path.exists("/tmp/keep.txt"), out


@needs_bwrap
async def test_linux_sandbox_masks_host_sockets_outside_tmp_and_run(ws):
    """沙箱看得见的位置上的套接字（家目录里的 Docker Desktop、colima、screen……）按 /proc/net/unix 逐个盖住：
    工作区挂回到 /tmp 下面也一样；绑定之后改过名的（/proc/net/unix 里还是旧名字）按原来的目录找出来；
    经 /tmp 里的符号链接绑定的（记下的路径在 /tmp 下，文件却在工作区里）按真实位置遮住。/var/tmp 不和宿主共用。"""
    (ws / "a").mkdir()
    (ws / "b").mkdir()
    link = os.path.join(tempfile.mkdtemp(prefix="fw", dir="/tmp"), "l")
    os.symlink(ws / "b", link)
    plain, renamed, linked = str(ws / "s"), str(ws / "a" / "s"), str(ws / "b" / "s")
    probe = 'python3 -c "import socket,sys; socket.socket(socket.AF_UNIX).connect(sys.argv[1]); print(\'CONNECTED\')" {} 2>&1 | tail -1'
    cmds = [*(probe.format(p) for p in (plain, renamed, linked)), "echo x > /var/tmp/fw-probe && ls /var/tmp"]
    h = make_harness(ws, [{"tool_calls": [{"name": "bash", "args": {"command": c}}]} for c in cmds] + ["好"],
                     permissions__mode="bypass", permissions__sandbox="linux")
    servers = [await asyncio.start_unix_server(lambda r, w: w.close(), p)
               for p in (plain, str(ws / "a" / "t"), os.path.join(link, "s"))]
    os.rename(ws / "a" / "t", renamed)
    try:
        async with servers[0], servers[1], servers[2]:
            out = [e["content"] for e in tool_ends((await h.run("连一下")).events)]
    finally:
        shutil.rmtree(os.path.dirname(link), ignore_errors=True)
    assert not any("CONNECTED" in o for o in out[:3]), out
    assert "fw-probe" in out[3] and not os.path.exists("/var/tmp/fw-probe"), out


@needs_bwrap
def test_linux_sandbox_confines_writes(tmp_path):
    from flywhl_harness.tools.sandbox import bwrap_argv

    ws, outside = tmp_path / "ws", tmp_path / "outside"
    ws.mkdir()
    outside.mkdir()
    subprocess.run(bwrap_argv(f"touch {outside}/x; touch {ws}/y", ws, writable=()), capture_output=True)
    assert not (outside / "x").exists() and (ws / "y").exists()


@needs_bwrap
@pytest.mark.parametrize("network,reachable", [("allow", True), ("deny", False)])
async def test_linux_sandbox_network_deny(ws, network, reachable):
    async def handle(reader, writer):
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    cmd = f"(exec 3<>/dev/tcp/127.0.0.1/{port}) 2>/dev/null && echo CONNECTED || echo BLOCKED; ls -A /run | wc -l"
    h = make_harness(ws, [{"tool_calls": [{"name": "bash", "args": {"command": cmd}}]}, "好"],
                     permissions__mode="bypass", permissions__sandbox="linux", permissions__sandbox_network=network)
    async with server:
        out = tool_ends((await h.run("连一下")).events)[0]["content"]
    assert ("CONNECTED" in out) is reachable, out
    if not reachable:   # /run 换成了空目录：docker.sock、D-Bus 的套接字都看不到
        assert out.split("\n")[1].strip() == "0", out


# ---------------------------------------------------------------------- 整条路径放在变量里
# 只调用 analyze()（纯判定），不执行任何命令
@pytest.mark.parametrize("cmd", [
    "D=~; rm -rf $D",                       # 赋过值的变量按取值展开
    "C=rm; $C -rf ~",                       # 命令名在变量里
    'A="-rf ~"; rm $A',                     # 不带引号的变量按空白拆开
    'C="rm -rf ~"; bash -c "$C"',
    'eval "D=~"; rm -rf $D',                # eval 在当前 shell 里赋值
    "X='rm -rf ~'; eval $X",
    "rm -rf //*", "rm -rf /./usr/..", "rm -rf ~other", "find $HOME/. -delete",
    "cd ~ && rm -rf *", "cd; rm -rf ./*", "cd /usr && rm -rf ../*", "pushd ~ && rm -rf *",
    "cd $X && rm -rf *",                    # X 为空时 cd 回到家目录
    "D=~ sh -c 'rm -rf $D'", "env D=~ sh -c 'rm -rf $D'",   # 前置赋值，子 shell 看得到
])
def test_paths_in_variables_and_cwd_are_resolved(cmd):
    assert analyze(cmd).catastrophic, cmd


@pytest.mark.parametrize("cmd,var", [
    ("rm -rf $DIR/*", "DIR"),
    ("X=$(mktemp -d); rm -rf $X/*", "X"),                # mktemp 失败时 X 是空的
    ("if c; then D=build; fi; rm -rf $D/*", "D"),        # 分支可能没走
    ("c && D=build; rm -rf $D/*", "D"),
    ("D=build | cat; rm -rf $D/*", "D"),                 # 管道、子 shell 里的赋值留不下来
    ("(D=build); rm -rf $D/*", "D"),
    ("D=x; sh -c 'rm -rf $D/*'", "D"),                   # 没导出：子 shell 里是空的
    ("trap 'rm -rf $D/*' EXIT; D=build", "D"),           # trap 可能在赋值之前触发
    ("find . -exec rm -rf $D/x {} +", "D"),
    ("taskset 1 rm -rf $D/*", "D"),
    ("chmod -R 777 $D/", "D"),
    ("DIR=build bash -c \"rm -rf $DIR/*\"", "DIR"),     # 双引号里的 $DIR 由外层展开，前置赋值管不到
])
def test_possibly_empty_variable_is_catastrophic_and_says_how_to_fix(cmd, var):
    a = analyze(cmd)
    assert a.catastrophic, cmd
    assert f"${{{var}:?}}" in a.catastrophic_reason, a.catastrophic_reason


@pytest.mark.parametrize("cmd", [
    "rm -rf ${DIR:?}/*",                    # 为空时 bash 报错、不执行
    "D=build; rm -rf $D/*",
    "D=build && rm -rf $D/*",
    "{ D=build; } > log; rm -rf $D/*",      # 花括号不开子 shell
    "for d in a b; do rm -rf $d/*; done",   # 循环变量在循环体里一定有值
    "export D=build; sh -c 'rm -rf $D/*'",
    "D=build; trap 'rm -rf $D/*' EXIT",
    "rm -rf '$D'/*", "rm -rf \\$D/*",       # 不展开的 $：就是名叫 $D 的目录
    'rm -rf ""',                            # 空参数：rm 报错，什么也不删
    "cd build && rm -rf *",
    "DIR=build bash -c 'rm -rf $DIR/*'",
])
def test_assigned_or_guarded_variables_are_not_catastrophic(cmd):
    a = analyze(cmd)
    assert not a.catastrophic, (cmd, a.catastrophic_reason)
    assert not any("\ue024" in r for r in a.reasons) and not any("\ue024" in s for s in a.segments)


def test_assembled_paths_hit_credential_and_boundary_checks(ws, tmp_path):
    assert analyze("D=$HOME/.ss; cat ${D}h/known_hosts").credential
    assert analyze("K=.ku; cat ~/${K}be/config").credential
    a = analyze("F=/etc/passwd; cat $F")
    assert any("/etc/passwd" in r for r in a.reasons), a.reasons
    assert analyze("cat ${X:-/etc}/passwd").level == "external"
    # 切换目录之后的相对路径按真实位置再查一次：链接在工作区外，字面上看不出指向凭证
    (tmp_path / "home" / ".ssh").mkdir(parents=True)
    (tmp_path / "home" / ".ssh" / "id_ed").write_text("x")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "notes").symlink_to(tmp_path / "home" / ".ssh" / "id_ed")
    assert analyze(f"cd {tmp_path / 'out'} && cat notes", str(ws)).credential
    assert analyze("cd sub && cat ../../x", str(ws)).level == "external"


def test_git_reads_the_repository_it_runs_in(ws):
    (ws / ".git").mkdir()
    (ws / ".git" / "config").write_text("[core]\n\tbare = false\n")
    (ws / "sub" / ".git").mkdir(parents=True)
    (ws / "sub" / ".git" / "config").write_text('[diff "y"]\n\ttextconv = id\n')
    assert analyze("git diff", str(ws)).level == "read"
    a = analyze("cd sub && git diff", str(ws))
    assert (a.level, a.opaque) == ("external", True), a.reasons
    a = analyze("cd $X && git status", str(ws))
    assert (a.level, a.opaque) == ("external", True), a.reasons


def test_globs_too_wide_to_check_fail_closed(ws):
    (ws / "many").mkdir()
    for i in range(1025):
        (ws / "many" / f"f{i}").touch()
    a = analyze("cat many/*", str(ws))
    assert a.level == "external" and any("1024" in r for r in a.reasons), a.reasons
    assert analyze("cat */*/*/*", str(ws)).level == "external"
    assert analyze("cat many/f1*", str(ws)).level == "read"


def test_self_referencing_values_do_not_stall_the_analysis():
    """取值里又引用变量、层层 bash -c：组合数是 16 的三次方。整棵递归共用一份预算，超了任何模式都拒绝——
    剩下的段没分析，后面可能就跟着 rm -rf ~。"""
    vals = ("A='bash -c \"$A $B; $B $A\"';" + ";".join(f"A=v{i}" for i in range(7)) + ";"
            "B='eval \"$B $A\"';" + ";".join(f"B=w{i}" for i in range(7)) + "; export A B; ")
    started = time.perf_counter()
    a = analyze(vals + "; ".join(['bash -c "$A $B; $B $A"'] * 16) + "; rm -rf ~")
    assert time.perf_counter() - started < 10
    assert a.catastrophic and "组合太多" in a.catastrophic_reason, a.catastrophic_reason


def test_truncated_value_combinations_fail_closed_on_risky_values():
    """取值组合超过 16 种时只逐个分析前 16 种：截掉的取值里有根目录、家目录这类值就拒绝，其余按无法静态分析处理。"""
    many = ";".join(f"A=a{i}" for i in range(8)) + ";" + ";".join(f"B=b{i}" for i in range(7))
    a = analyze(many + "; B=~; rm -rf $A $B")
    assert a.catastrophic and "~" in a.catastrophic_reason, a.catastrophic_reason
    a = analyze("for f in a b c d e f g h i j; do cp $f $f.bak; done")
    assert a.level == "external" and not a.catastrophic, a.reasons


@pytest.mark.parametrize("cmd", [
    "D=build; read -raD <<< x; rm -rf $D/*",      # 选项与变量名写在一起
    "D=build; wait -p D; rm -rf $D/*",
    "D=build; printf -vD x; rm -rf $D/*",
    "D=build; source env.sh; rm -rf $D/*",         # 脚本里改了什么看不到
    "IFS=x; C=rmx-rfx~; $C",                       # 改了 IFS，展开后按 x 拆词
    "IFS=$(printf x); C=y; $C",
    "alias ls='rm -rf ~'; ls",
    "source env.sh; D=a:/; rm -rf $D",            # 脚本可能改了 IFS：按标点拆出根目录
    "source env.sh; C=rm:-rf:/; $C",
])
def test_hidden_variable_changes_are_not_trusted(cmd):
    a = analyze(cmd)
    assert a.catastrophic, (cmd, a.reasons)


def test_source_does_not_make_every_expansion_fatal():
    """source 之后 IFS 可能被改过，但只补按标点拆的几种：之后的变量展开照常可用，字母当 IFS 属于刻意混淆。"""
    for cmd in ("source .venv/bin/activate && echo $VIRTUAL_ENV && pytest $ARGS",
                "source a.sh; source b.sh; X=1; echo $X",
                "source env.sh; D=/tmp/build; rm -rf $D"):
        a = analyze(cmd)
        assert not a.catastrophic, (cmd, a.catastrophic_reason)


@pytest.mark.parametrize("cmd", [
    "shopt -s expand_aliases\nalias r='rm -rf'\nr ~",
    "alias r='rm -rf'; r ~",
    "alias s='sudo '; alias r='rm -rf'; s r ~",     # 以空格结尾的别名，下一个词同样按别名展开
    "BASH_ALIASES[r]='rm -rf'; r ~",
    'f() { rm -rf "$@"; }; f ~',
    "function f { rm -rf $1/*; }; f /",
    'f() ( rm -rf "$1" ); f ~',
    "eval 'g() { rm -rf \"$@\"; }'; g ~",
    'f() { g "$@"; }; g() { rm -rf "$@"; }; f ~',
])
def test_aliases_and_functions_are_checked_where_they_are_called(cmd):
    """定义处只看得到 rm -rf 和位置参数，删什么要到调用处才知道：接上这次的参数再分析一遍。"""
    a = analyze(cmd)
    assert a.catastrophic and ("~" in a.catastrophic_reason or "/" in a.catastrophic_reason), (cmd, a.reasons)


def test_harmless_aliases_and_functions_stay_harmless():
    for cmd in ("alias ll='ls -l'; ll", "alias ls='ls -F'; ls", 'f() { rm -rf "$@"; }; f build', "f() { f; }; f",
                "f() { echo hi; }; f > out.txt"):
        a = analyze(cmd)
        assert not a.catastrophic, (cmd, a.catastrophic_reason)


def test_truncated_values_are_checked_against_the_current_directory():
    """截掉的取值按可能的当前目录判断：cd /tmp 之后的 ../* 就是 /*。一个词里的组合截断同样检查。"""
    many_a = ";".join(f"A=x{i}" for i in range(7))
    many_b = ";".join(f"B=y{i}" for i in range(7))
    a = analyze(f"cd /tmp; {many_a}; A=..; {many_b}; B=/*; rm -rf $A$B")
    assert a.catastrophic and "组合太多" in a.catastrophic_reason, a.reasons
    a = analyze(f"cd /tmp; {many_a}; A=../*; {many_b}; B=y7; rm -rf $A $B")   # ../* 排在第 57 种之后
    assert a.catastrophic and "../*" in a.catastrophic_reason, a.reasons
