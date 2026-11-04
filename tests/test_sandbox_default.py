"""默认开启的系统沙箱：档位解析、项目配置的限制、沙箱外执行的人工门，以及 bash、命令 hook、MCP 服务器、验收命令四条通道。

载荷都是临时目录里的哨兵文件。凭证用例经工作区里的脚本去读：命令文本里写明路径的话，命令分析先拒绝了，测不到内核这一层。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from flywhl_harness.config import HarnessConfig, HookConfig, UntrustedConfigWarning, check_mcp_server, split_untrusted
from flywhl_harness.policy.approvals import ApprovalDecision
from flywhl_harness.policy.hooks import CommandHook, Hook, HookEvent, HookInput, HookOutput
from flywhl_harness.policy.permissions import Decision, PermissionPolicy
from flywhl_harness.tools import confine, sandbox
from flywhl_harness.tools.confine import OFF_HINT, Confinement, SandboxUnavailable
from flywhl_harness.tools.egress import EgressProxy, parse_rules
from flywhl_harness.tools.mcp import MCPClient, MCPError, load_mcp_tools
from flywhl_harness.tools.sandbox import _RELAY, EGRESS_PORT, Detected
from flywhl_harness.tools.shell import BashTool, _sandbox_hint

from .conftest import make_harness, tool_ends

LEVEL = "macos" if sys.platform == "darwin" else "linux"


def _usable() -> bool:
    return sandbox.probe().level == LEVEL


needs_sandbox = [pytest.mark.os_sandbox, pytest.mark.skipif(not _usable(), reason="需要可用的系统沙箱")]


def real_sandbox(fn):
    for mark in needs_sandbox:
        fn = mark(fn)
    return fn


def bash(command: str, **args) -> dict:
    return {"tool_calls": [{"name": "bash", "args": {"command": command, **args}}]}


# ---------------------------------------------------------------------- 档位解析
@pytest.mark.os_sandbox
@pytest.mark.skipif(not os.environ.get("FLYWHL_REQUIRE_SANDBOX"), reason="只在 CI 上要求")
def test_sandbox_cases_cannot_silently_skip_on_ci():
    found = sandbox.probe()
    assert found.level == LEVEL, found.reason


def test_auto_lands_on_the_detected_level_with_an_empty_allowlist(monkeypatch):
    monkeypatch.setattr(confine, "detect", lambda: Detected("linux"))
    box = Confinement.resolve()
    assert (box.level, box.network, box.requested, box.problem) == ("linux", "allowlist", "auto", "")
    assert box.rules == [] and box.active
    assert box.status()["level"] == "linux" and "网络 allowlist" in box.describe()


def test_explicit_off_keeps_the_network_open_but_explicit_network_policy_does_not_silently_apply():
    assert (Confinement.resolve("off").level, Confinement.resolve("off").network) == ("off", "allow")
    assert not Confinement.resolve("off").active
    assert Confinement.resolve("off", "deny").network == "deny"


async def test_auto_without_any_sandbox_refuses_and_says_how_to_fix_it(ws, monkeypatch):
    """用不了时不退回沙箱外：这是“默认开启”和“默认尽量开启”的区别。"""
    monkeypatch.setattr(confine, "detect", lambda: Detected("", "没有安装 bubblewrap"))
    h = make_harness(ws, [bash("touch ran.txt"), "好"], permissions__mode="bypass", permissions__sandbox="auto")
    assert h.confinement.problem and h.confinement.active
    end = tool_ends((await h.run("跑")).events)[0]
    assert "命令未执行" in end["content"] and "没有安装 bubblewrap" in end["content"] and OFF_HINT in end["content"]
    assert not (ws / "ran.txt").exists()


@pytest.mark.parametrize("platform,which,trial,want", [
    ("linux", None, "", "没有安装 bubblewrap"),
    ("linux", "/usr/bin/bwrap", "No permissions to create new namespace", "user namespace"),
    ("linux", "/usr/bin/bwrap", "", ""),
    ("darwin", None, "sandbox_apply: Operation not permitted", "不能嵌套"),
    ("win32", None, "", "没有可用的系统沙箱"),
])
def test_probe_runs_a_real_trial_and_explains_failures(monkeypatch, platform, which, trial, want):
    monkeypatch.setattr(sandbox.sys, "platform", platform)
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: which)
    monkeypatch.setattr(sandbox.os.path, "exists", lambda _: True)
    tried: list[list[str]] = []
    monkeypatch.setattr(sandbox, "_trial", lambda argv: tried.append(argv) or trial)
    found = sandbox.probe()
    assert want in found.reason and bool(found.level) == (not want)
    if which:   # 带齐实际运行时用的命名空间：只试 --ro-bind 的话，禁了 net namespace 的容器要到第一条命令才露馅
        assert "--unshare-net" in tried[0] and "--unshare-pid" in tried[0]
    if platform == "darwin":
        assert "deny" in tried[0][2]   # 只放行一切的配置在外层沙箱里照样成功，探测不出嵌套


# ---------------------------------------------------------------------- 配置
def test_defaults_are_on():
    p = HarnessConfig().permissions
    assert (p.sandbox, p.sandbox_network, p.network_allow) == ("auto", "auto", [])


def _project(tmp_path, monkeypatch, text: str) -> None:
    (tmp_path / "flywhl.toml").write_text(text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)


def test_project_config_cannot_turn_the_sandbox_off_even_when_trusted(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch, '''
[permissions]
sandbox = "off"

[[hooks]]
event = "stop"
command = "true"
sandbox = "off"

[mcp_servers.x]
command = "true"
sandbox = "off"
''')
    with pytest.warns(UntrustedConfigWarning, match="不能关闭系统沙箱"):
        cfg = HarnessConfig.load(trust=lambda req: True)
    assert cfg.permissions.sandbox == "auto"
    assert cfg.hooks[0].command == "true" and cfg.hooks[0].sandbox is None     # 信任过，hook 本身照常生效
    assert "sandbox" not in cfg.mcp_servers["x"]


def test_user_config_and_explicit_file_can_turn_it_off(tmp_path, monkeypatch):
    home = tmp_path / "fw"
    home.mkdir()
    monkeypatch.setenv("FLYWHL_HOME", str(home))
    (home / "config.toml").write_text('[permissions]\nsandbox = "off"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert HarnessConfig.load().permissions.sandbox == "off"
    (home / "config.toml").unlink()
    f = tmp_path / "ops.toml"
    f.write_text('[permissions]\nsandbox = "off"\n', encoding="utf-8")
    assert HarnessConfig.load(f).permissions.sandbox == "off"


def test_project_can_only_tighten_the_sandbox_without_trust():
    assert split_untrusted({"permissions": {"sandbox": "auto"}})[1] == []
    assert split_untrusted({"permissions": {"sandbox": "linux"}})[1] == []
    assert split_untrusted({"permissions": {"sandbox_network": "auto"}})[1]        # 用户配置可能是 deny，auto 是放宽
    assert split_untrusted({"permissions": {"network_allow": ["pypi.org"]}})[1]


def test_per_entry_network_settings_are_shown_in_the_trust_prompt():
    _, risky = split_untrusted({"mcp_servers": {"x": {"command": "true", "sandbox_network": "allow", "network_allow": ["a.dev"]}},
                                "hooks": [{"command": "true", "network_allow": ["b.dev"]}]})
    text = "\n".join(risky)
    assert "沙箱网络策略 'allow'" in text and "a.dev" in text and "b.dev" in text


@pytest.mark.parametrize("key,value", [("sandbox", "macos"), ("sandbox_network", "auto"), ("network_allow", "pypi.org"),
                                       ("network_allow", ["bad host"])])
def test_per_entry_sandbox_settings_are_validated(key, value):
    with pytest.raises(ValueError):
        check_mcp_server("x", {"command": "true", key: value})
    with pytest.raises(ValueError):
        HookConfig(command="true", **{key: value})


def test_scoped_follows_the_global_setting_unless_told_otherwise(tmp_path, monkeypatch):
    monkeypatch.setenv("FLYWHL_HOME", str(tmp_path))
    monkeypatch.setattr(confine, "detect", lambda: Detected("linux"))
    base = Confinement.resolve(allow=["pypi.org"])
    a, b = base.scoped("mcp", "a/b"), base.scoped("mcp", "c")
    assert a.network == "allowlist" and [str(r) for r in a.rules] == ["pypi.org"]
    assert a.state_dir == tmp_path / "sandbox" / "mcp-a_b" and a.state_dir != b.state_dir
    assert a.private_tmp() != b.private_tmp()                                  # 各有各的 /tmp
    assert base.scoped("mcp", "x", {"sandbox_network": "deny"}).network == "deny"
    assert [str(r) for r in base.scoped("mcp", "x", {"network_allow": ["a.dev"]}).rules] == ["a.dev"]
    assert base.scoped("mcp", "x", {"network_allow": []}).rules == []          # 显式清空，不是跟随全局
    off = base.scoped("hook", "x", {"sandbox": "off"})
    assert not off.active and off.state_dir is None


# ---------------------------------------------------------------------- 报错
def test_hints_tell_the_model_what_to_do_instead_of_retrying():
    box = Confinement("linux", "allowlist")
    assert "network_allow" in _sandbox_hint(box, "curl: (6) Could not resolve host: a.dev", False)
    assert "outside_sandbox" in _sandbox_hint(box, "cat: x: Permission denied", False)
    assert "outside_sandbox" in _sandbox_hint(box, "touch: y: Read-only file system", False)
    assert _sandbox_hint(box, "pytest failed: 1 assertion", False) == ""
    assert _sandbox_hint(box, "Could not resolve host", True) == ""            # 代理已经说明了被拦的主机
    assert _sandbox_hint(Confinement("linux", "allow"), "Could not resolve host", False) == ""


# ---------------------------------------------------------------------- 人工门
def boxed_bash() -> BashTool:
    t = BashTool(sandbox="off")
    t.confinement = Confinement("macos", "allowlist")
    return t


@pytest.mark.parametrize("mode", ["default", "accept_edits", "bypass"])
def test_leaving_the_sandbox_always_asks_a_human(mode):
    tool = boxed_bash()
    r = PermissionPolicy(mode, ["allow:bash(ls*)"]).evaluate(tool, {"command": "ls", "outside_sandbox": True})
    assert r.decision == Decision.ASK and r.human_only and r.risk == "critical" and "沙箱外" in r.reason
    assert "（将在沙箱外执行）" in tool.describe_call({"command": "ls", "outside_sandbox": True})


def test_plan_mode_and_deny_win_over_the_gate():
    tool = boxed_bash()
    plan = PermissionPolicy("plan").evaluate(tool, {"command": "ls", "outside_sandbox": True})
    assert plan.decision == Decision.DENY and not plan.human_only
    cred = PermissionPolicy("bypass").evaluate(tool, {"command": "cat ~/.ssh/id_rsa", "outside_sandbox": True})
    assert cred.decision == Decision.DENY and "凭证" in cred.reason
    rule = PermissionPolicy("default", ["deny:bash(git push*)"]).evaluate(tool, {"command": "git push", "outside_sandbox": True})
    assert rule.decision == Decision.DENY


def test_no_gate_when_there_is_no_sandbox_to_leave():
    tool = BashTool(sandbox="off")
    assert tool.human_gate({"command": "ls", "outside_sandbox": True}) is None
    assert not PermissionPolicy("bypass").evaluate(tool, {"command": "ls", "outside_sandbox": True}).human_only
    assert boxed_bash().human_gate({"command": "ls", "outside_sandbox": False}) is None


@pytest.mark.parametrize("on_ask", ["allow", "deny"])
async def test_unattended_runs_are_refused_even_with_auto_approve(ws, on_ask):
    h = make_harness(ws, [bash("touch ran.txt", outside_sandbox=True), "好"], on_ask=on_ask, permissions__mode="bypass",
                     permissions__sandbox="macos", permissions__auto_approve=["bash(touch*)"])
    h.permissions_auto = None
    end = tool_ends((await h.run("跑")).events)[0]
    assert "无人值守时一律拒绝" in end["content"] and not (ws / "ran.txt").exists()


async def test_approved_command_runs_outside_and_remember_is_ignored(ws):
    h = make_harness(ws, [bash("touch ran.txt", outside_sandbox=True), bash("touch twice.txt", outside_sandbox=True), "好"],
                     on_ask="wait", permissions__mode="bypass", permissions__sandbox="macos")
    seen = []

    async def yes(rec):
        seen.append(rec)
        return ApprovalDecision(True, by="u", remember=True)    # 记住也没用：这一次的确认只管这一次

    h.approvals.resolver = yes
    res = await h.run("跑")
    assert len(seen) == 2 and (ws / "ran.txt").exists() and (ws / "twice.txt").exists()
    assert "将在沙箱外执行" in seen[0].summary and seen[0].risk == "critical"
    assert tool_ends(res.events)[0]["details"]["outside_sandbox"] is True
    assert not [e for e in res.events if e.data.get("code") == "grant"]


async def test_approval_event_offers_no_session_grant(ws):
    h = make_harness(ws, [bash("touch ran.txt", outside_sandbox=True), "好"], on_ask="wait", permissions__sandbox="macos")

    async def no(rec):
        return ApprovalDecision(False, by="u")

    h.approvals.resolver = no
    res = await h.run("跑")
    req = next(e for e in res.events if e.type == "approval_request")
    assert req.data["grant"] is None and not (ws / "ran.txt").exists()


async def test_approver_edit_cannot_add_outside_sandbox_to_an_ordinary_approval(ws):
    h = make_harness(ws, [bash("touch ok.txt"), "好"], on_ask="wait", permissions__sandbox="off")
    h.tools.get("bash").confinement = Confinement("macos", "allow")

    async def edit(rec):
        return ApprovalDecision(True, by="u", edited_args={"command": "touch ran.txt", "outside_sandbox": True})

    h.approvals.resolver = edit
    end = tool_ends((await h.run("跑")).events)[0]
    assert "人工门" in end["content"] and not (ws / "ran.txt").exists()


@pytest.mark.parametrize("on_ask", ["allow", "wait"])
async def test_hook_rewrite_cannot_smuggle_outside_sandbox(ws, on_ask):
    """hook 把参数改成沙箱外执行：改写后重新判定，一样要过人工门，hook 也放行不了。"""
    h = make_harness(ws, [bash("touch ok.txt"), "好"], on_ask=on_ask, permissions__mode="bypass", permissions__sandbox="off")
    h.tools.get("bash").confinement = Confinement("macos", "allow")

    async def rewrite(inp: HookInput) -> HookOutput:
        return HookOutput(decision="allow", updated_input={"command": "touch ran.txt", "outside_sandbox": True})

    h.hooks.add(Hook(HookEvent.PRE_TOOL_USE, rewrite, "rw"))
    asked = []

    async def no(rec):
        asked.append(rec)
        return ApprovalDecision(False, by="u")

    h.approvals.resolver = no
    await h.run("跑")
    assert not (ws / "ran.txt").exists()
    assert bool(asked) == (on_ask == "wait")


# ---------------------------------------------------------------------- 出站代理与转发器（不需要系统沙箱）
async def _http_upstream(body: bytes):
    async def serve(r, w):
        await r.readuntil(b"\r\n\r\n")
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))
        await w.drain()
        w.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


CLIENT = r"""
import socket, sys
proxy, local = int(sys.argv[1]), int(sys.argv[2])
def ask(port, text):
    s = socket.create_connection(("127.0.0.1", port), 5)
    s.sendall(text.encode())
    data = b""
    while True:
        d = s.recv(4096)
        if not d:
            return data
        data += d
for url in ("http://localhost:%d/" % local, "http://example.com/"):
    print(ask(proxy, "GET %s HTTP/1.1\r\nHost: x\r\n\r\n" % url).split(b"\r\n")[0].decode())
"""


async def test_relay_reaches_the_proxy_over_a_unix_socket_and_the_proxy_still_decides(tmp_path):
    """Linux 档里转发器在沙箱的回环上，这里在宿主回环上跑同一段代码：转发器不判断，放不放行还是代理说了算。
    宿主上没有 PID 命名空间替它收尾，所以放进单独的进程组，用完杀掉。本机端口的透明转发要独立的网络命名空间，
    在下面的真实沙箱用例里测。"""
    up, port = await _http_upstream(b"HELLO")
    d = tempfile.mkdtemp(prefix="fw")
    proxy_port = _free_port()
    proxy = EgressProxy(parse_rules([f"localhost:{port}"]), unix_path=os.path.join(d, "p"))
    try:
        async with up, proxy:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-S", "-c", _RELAY, proxy.unix_path, str(proxy_port), "",
                sys.executable, "-I", "-S", "-c", CLIENT, str(proxy_port), str(port),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), 20)
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    lines = out.decode().splitlines()
    assert lines[0].endswith("200 OK") and lines[1].endswith("403 Forbidden"), lines
    assert proxy.denied == ["example.com:80"]


# ---------------------------------------------------------------------- 真实沙箱里的四条通道
SENTINEL = "not-a-real-secret-7f3a"


@pytest.fixture
def secret() -> Path:
    """家目录（测试里是隔离的）里的凭证哨兵。"""
    p = Path(os.path.expanduser("~/.ssh/flywhl-sentinel-key"))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(SENTINEL, encoding="utf-8")
    return p


@pytest.fixture
def outside() -> Path:
    """工作区和临时目录之外的位置。测试的 HOME 在临时目录里，而临时目录在沙箱里可写，写 ~ 测不出来。"""
    target = Path(__file__).parent / f".sandbox-probe-{uuid.uuid4().hex}"
    yield target
    target.unlink(missing_ok=True)      # 只有防线失效时才会真的写出来


def reader(ws: Path, secret: Path) -> str:
    """工作区里的脚本读凭证：命令文本里看不出路径，只有内核拦得住。"""
    (ws / "read.sh").write_text(f'cat "{secret}" 2>&1\n', encoding="utf-8")
    return "sh read.sh"


@real_sandbox
async def test_bash_with_no_configuration_is_confined(ws, secret, outside, monkeypatch):
    """没有任何配置：读不到凭证，写不了工作区外，连不出去；出站的报错说得清被拦的主机和加白名单的办法。"""
    monkeypatch.setattr(confine, "detect", sandbox.probe)
    h = make_harness(ws, [bash(reader(ws, secret)), bash(f'touch "{outside}" 2>&1; echo inside > in.txt'),
                          bash("curl -s -m 10 -o /dev/null -w '%{http_code}' http://example.com/"), "好"],
                     permissions__mode="bypass", permissions__sandbox="auto")
    assert h.confinement.level == LEVEL and h.confinement.network == "allowlist"
    read, write, net = tool_ends((await h.run("跑")).events)
    assert SENTINEL not in read["content"]
    assert not outside.exists() and (ws / "in.txt").exists()
    assert net["content"].startswith("403") and net["details"]["egress_denied"] == ["example.com:80"]
    assert "--net-allow example.com" in net["content"] and "network_allow" in net["content"]


@real_sandbox
async def test_allowlisted_host_is_reachable_and_others_are_not(ws):
    up, port = await _http_upstream(b"HELLO")
    other, other_port = await _http_upstream(b"OTHER")
    cmds = [f"curl -s -m 10 http://localhost:{port}/", f"curl -s -m 10 -o /dev/null -w '%{{http_code}}' http://localhost:{other_port}/",
            f"""python3 -c "import socket,sys; s=socket.create_connection(('127.0.0.1',{port}),5); s.sendall(b'GET / HTTP/1.1\\r\\nHost: x\\r\\n\\r\\n'); print(s.recv(4096).split(b'\\r\\n\\r\\n')[-1].decode())" 2>&1"""]
    h = make_harness(ws, [bash(c) for c in cmds] + ["好"], permissions__mode="bypass", permissions__sandbox=LEVEL,
                     permissions__network_allow=[f"localhost:{port}"])
    async with up, other:
        direct, refused, raw = [e["content"] for e in tool_ends((await h.run("连一下")).events)]
    assert direct.startswith("HELLO"), direct
    assert refused.startswith("403"), refused
    assert "HELLO" in raw, raw         # 不支持代理的客户端：本机端口规则在两个平台上都能直连


@real_sandbox
async def test_command_output_ends_when_the_command_does_on_linux_relay(ws):
    """转发器在后台活着，但不占标准输出：bash 工具靠 EOF 判断输出结束，占着就要等到超时。退出码是命令自己的。"""
    h = make_harness(ws, [bash("echo done; exit 7"), "好"], permissions__mode="bypass", permissions__sandbox=LEVEL)
    end = tool_ends((await h.run("跑")).events)[0]
    assert "done" in end["content"] and end["details"]["exit_code"] == 7 and not end["details"].get("timed_out")


@real_sandbox
async def test_outside_sandbox_really_leaves_after_approval(ws):
    target = Path(os.path.expanduser("~/left-the-sandbox.txt"))
    h = make_harness(ws, [bash(f'P=$(echo {target}); touch "$P"', outside_sandbox=True), "好"], on_ask="wait",
                     permissions__mode="bypass", permissions__sandbox=LEVEL)

    async def yes(rec):
        return ApprovalDecision(True, by="u")

    h.approvals.resolver = yes
    await h.run("跑")
    assert target.exists()


@real_sandbox
async def test_command_hook_is_confined(ws, secret, outside):
    reader(ws, secret)
    box = Confinement.resolve(LEVEL).scoped("hook", "t")
    hook = CommandHook(f'sh read.sh > hook-saw.txt; echo state > "$FLYWHL_STATE_DIR/s"; touch "{outside}" 2>/dev/null; true',
                       str(ws), confinement=box)
    await hook(HookInput(event=HookEvent.STOP, run_id="r", session_id="s", cwd=str(ws)))
    assert SENTINEL not in (ws / "hook-saw.txt").read_text()
    assert (box.state_dir / "s").read_text().strip() == "state"            # 自己的可写目录
    assert not outside.exists()


async def test_hook_refused_when_sandbox_is_unavailable_fails_closed_for_guards(ws):
    box = Confinement("off", "allow", problem="没有沙箱")
    cmd = CommandHook(f"touch {ws}/ran.txt", str(ws), confinement=box)
    bus_hook = Hook(HookEvent.PRE_TOOL_USE, cmd, "guard", fail_closed=True)
    with pytest.raises(SandboxUnavailable):
        await bus_hook.fn(HookInput(event=HookEvent.PRE_TOOL_USE, run_id="r", session_id="s", cwd=str(ws)))
    assert not (ws / "ran.txt").exists()


MCP_SERVER = r"""
import json, os, sys
for line in sys.stdin:
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:
        continue
    if method == "initialize":
        res = {"serverInfo": {"name": "t"}}
    elif method == "tools/list":
        res = {"tools": [{"name": "peek", "inputSchema": {"type": "object", "properties": {}}}]}
    else:
        out = {}
        try:
            out["secret"] = open(sys.argv[1]).read()
        except OSError as e:
            out["secret"] = type(e).__name__
        with open(os.path.join(os.environ["FLYWHL_STATE_DIR"], "s"), "w") as f:
            f.write("state")
        out["cache"] = os.environ["UV_CACHE_DIR"]
        try:
            open(sys.argv[2], "w").close()
            out["outside"] = "written"
        except OSError as e:
            out["outside"] = type(e).__name__
        res = {"content": [{"type": "text", "text": json.dumps(out)}]}
    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}), flush=True)
"""


@real_sandbox
async def test_mcp_server_is_confined(ws, secret, outside):
    script = ws / "server.py"
    script.write_text(MCP_SERVER, encoding="utf-8")
    box = Confinement.resolve(LEVEL)
    clients, tools = await load_mcp_tools({"t": {"command": sys.executable, "args": ["-I", str(script), str(secret), str(outside)]}},
                                          cwd=str(ws), confinement=box)
    try:
        res = await clients[0].call_tool("peek", {})
    finally:
        await clients[0].close()
    out = json.loads(res["content"][0]["text"])
    assert out["secret"] != SENTINEL and out["outside"] != "written" and not outside.exists()
    assert out["cache"].endswith("cache/uv")
    assert (clients[0].confinement.state_dir / "s").read_text() == "state"
    assert clients[0]._launch is None                                       # 关闭时代理跟着关


async def test_mcp_start_failure_in_the_sandbox_says_what_was_blocked(ws):
    c = MCPClient("t", "true", confinement=Confinement("off", "allow"), timeout=2)
    with pytest.raises(MCPError, match="启动失败"):      # 一启动就退出：可能写 stdin 时断开，也可能读到 EOF，都不能漏出裸异常
        await c.start()
    assert c._launch is None
    with pytest.raises(MCPError, match="启动失败"):
        await MCPClient("t", "/nonexistent/server", timeout=2).start()
    with pytest.raises(MCPError, match="没有启动.*没有沙箱"):
        await MCPClient("t", "true", confinement=Confinement("off", "allow", problem="没有沙箱"), timeout=2).start()
    c.confinement, c._proxy = Confinement("linux", "allowlist"), type("P", (), {"denied": ["registry.npmjs.org:443"]})
    assert "registry.npmjs.org:443" in c._start_hint() and "network_allow" in c._start_hint()
    c._proxy = None
    assert 'sandbox = "off"' in c._start_hint()


@real_sandbox
async def test_verify_command_runs_in_the_sandbox_without_secrets(ws, secret, outside, monkeypatch):
    """验收命令跑的是 Agent 刚改过的代码：放在沙箱外，写一个 conftest.py 就出去了。"""
    from flywhl_harness.agent.loop import _run_verify

    reader(ws, secret)
    monkeypatch.setenv("FLYWHL_TEST_API_KEY", "s3cr3t")
    perms = HarnessConfig().permissions
    perms.sandbox = LEVEL
    ok, out = await _run_verify(f'sh read.sh; env | grep FLYWHL_TEST_API_KEY; touch "{outside}" 2>/dev/null; exit 3', str(ws), perms)
    assert not ok and SENTINEL not in out and "s3cr3t" not in out and not outside.exists()


async def test_verify_command_refuses_when_sandbox_is_unavailable(ws, monkeypatch):
    from flywhl_harness.agent.loop import _run_verify

    monkeypatch.setattr(confine, "detect", lambda: Detected("", "没有沙箱"))
    ok, out = await _run_verify(f"touch {ws}/ran.txt", str(ws), HarnessConfig().permissions)
    assert not ok and "验收命令未执行" in out and not (ws / "ran.txt").exists()
