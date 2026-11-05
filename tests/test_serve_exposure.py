"""``flywhl serve`` 的网络暴露：监听地址、令牌、Host 白名单，以及 ``--no-token`` 时沙箱里的命令连不连得到控制台。

这些用例不真正监听端口：走真实的命令行入口，把 ``uvicorn.Server.serve`` 换掉，拿到构造好的 app 再发请求。
"""

from __future__ import annotations

import ipaddress

import httpx
import pytest
from fastapi.testclient import TestClient

from flywhl_harness.config import HookConfig
from flywhl_harness.server.exposure import (console_hosts, console_reachers, console_url, is_wildcard, no_token_refusal,
                                            rules_reach_loopback, serve_refusal)
from flywhl_harness.tools.confine import Confinement
from flywhl_harness.tools.egress import EgressDenied, EgressProxy, parse_rules

from .conftest import make_harness

TOKEN = "t" * 16
PORT = 8765


@pytest.fixture
def serve(monkeypatch, ws):
    """运行 ``flywhl serve ...``，返回 (退出码, app)。被拒绝时 app 是 None。"""
    import uvicorn

    from flywhl_harness.cli import main

    def run(*argv: str):
        built = []

        async def fake_serve(self):
            built.append(self.config.app)

        monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
        with pytest.raises(SystemExit) as e:
            main(["serve", "--provider", "mock", "-w", str(ws), *argv])
        return e.value.code, (built[0] if built else None)

    return run


@pytest.fixture
def config(tmp_path):
    """写一份 ``-c`` 指定的配置（视为可信），返回命令行参数。"""

    def write(text: str) -> list[str]:
        path = tmp_path / "exposure.toml"
        path.write_text(text, encoding="utf-8")
        return ["-c", str(path)]

    return write


def status(app, host: str, token: str | None = TOKEN) -> int:
    headers = {"Host": host, **({"Authorization": f"Bearer {token}"} if token else {})}
    return TestClient(app).get("/api/sessions", headers=headers).status_code


# ---------------------------------------------------------------------- 监听非回环地址要显式声明
@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "box.lan"])
def test_listening_off_loopback_requires_expose(serve, capsys, host):
    code, app = serve("--host", host)
    assert code == 2 and app is None
    assert "--expose" in capsys.readouterr().err


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_needs_no_expose(serve, host):
    code, app = serve("--host", host)
    assert code == 0 and app is not None


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20"])
def test_expose_still_refuses_no_token(serve, capsys, host):
    code, app = serve("--host", host, "--expose", "--no-token")
    assert code == 2 and app is None
    assert "回环地址" in capsys.readouterr().err


@pytest.mark.parametrize("token", ["", "short", "x" * 15])
def test_short_token_is_refused_off_loopback(serve, capsys, token):
    code, app = serve("--host", "0.0.0.0", "--expose", "--token", token)
    assert code == 2 and app is None
    assert "16" in capsys.readouterr().err


def test_short_token_is_still_accepted_on_loopback(serve):
    code, app = serve("--token", "short")
    assert code == 0 and status(app, "127.0.0.1:8765", "short") == 200


def test_token_of_minimum_length_is_accepted_off_loopback(serve):
    code, app = serve("--host", "0.0.0.0", "--expose", "--token", "x" * 16)
    assert code == 0 and app is not None


# ---------------------------------------------------------------------- Host 白名单不再关闭
@pytest.mark.parametrize(("host", "literal"), [("0.0.0.0", "0.0.0.0:8765"), ("::", "[::]:8765")])
def test_wildcard_listen_keeps_the_host_allowlist(serve, host, literal):
    """早期版本监听所有网卡时把 ``*`` 放进白名单：DNS 重绑定这道防线随之消失，只剩共享令牌。"""
    code, app = serve("--host", host, "--expose", "--token", TOKEN)
    assert code == 0
    assert status(app, "evil.example") == 403
    assert status(app, literal) == 403
    assert status(app, "127.0.0.1:8765") == 200
    assert status(app, "127.0.0.1:8765", None) == 401, "Host 通过之后令牌照样要校验"


def test_wildcard_listen_trusts_only_what_allow_host_names(serve):
    code, app = serve("--host", "0.0.0.0", "--expose", "--token", TOKEN, "--allow-host", "Box.LAN")
    assert code == 0
    assert status(app, "box.lan:8765") == 200
    assert status(app, "evil.example") == 403


@pytest.mark.parametrize("host", ["192.168.1.20", "2001:db8::1"])
def test_specific_address_joins_the_allowlist(serve, host):
    code, app = serve("--host", host, "--expose", "--token", TOKEN)
    assert code == 0
    assert status(app, f"[{host}]:8765" if ":" in host else f"{host}:8765") == 200
    assert status(app, "evil.example") == 403


@pytest.mark.parametrize("argv", [(), ("--host", "0.0.0.0", "--expose", "--token", TOKEN)])
def test_allow_host_star_is_refused(serve, capsys, argv):
    code, app = serve(*argv, "--allow-host", "box.lan", "--allow-host", "*")
    assert code == 2 and app is None
    assert "--allow-host" in capsys.readouterr().err


def test_create_app_refuses_star(ws):
    from flywhl_harness.server.app import create_app

    with pytest.raises(ValueError, match="allowed_hosts"):
        create_app(make_harness(ws, ["好"]), auto_recover=False, token=TOKEN, allowed_hosts=["box.lan", "*"])


def test_wildcard_without_allow_host_prints_a_hint(serve, capsys):
    assert serve("--host", "0.0.0.0", "--expose", "--token", TOKEN)[0] == 0
    assert "--allow-host <主机名或IP>" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [("--host", "0.0.0.0", "--allow-host", "box.lan"), ("--host", "192.168.1.20"), ("--host", "127.0.0.1")])
def test_no_hint_when_nothing_is_missing(serve, capsys, argv):
    assert serve(*argv, "--expose", "--token", TOKEN)[0] == 0
    assert "--allow-host <主机名或IP>" not in capsys.readouterr().out


@pytest.mark.parametrize(("host", "shown"), [("0.0.0.0", "http://127.0.0.1:8765/"), ("::", "http://[::1]:8765/")])
def test_wildcard_listen_prints_a_link_the_allowlist_accepts(serve, capsys, host, shown):
    """通配地址不在白名单里：照原样打印的链接一打开就是 403。"""
    assert serve("--host", host, "--expose", "--token", TOKEN)[0] == 0
    assert f"{shown}?token={TOKEN}" in capsys.readouterr().out


# ---------------------------------------------------------------------- --no-token：沙箱里的命令不能批准自己的人工门
HOOK = '[[hooks]]\nevent = "pre_tool_use"\ncommand = "exit 0"\n'
MCP = '[mcp_servers.demo]\ncommand = "true"\n'
BOX = '[permissions]\nsandbox = "macos"\n'

REACHABLE = {
    "全局沙箱关闭": '[permissions]\nsandbox = "off"\n',
    "全局网络放开": BOX + 'sandbox_network = "allow"\n',
    "全局白名单写了 localhost": BOX + 'sandbox_network = "allowlist"\nnetwork_allow = ["localhost:8765"]\n',
    "全局白名单写了 127.0.0.1": BOX + 'network_allow = ["127.0.0.1:8765"]\n',
    "全局白名单放开本机所有端口": BOX + 'network_allow = ["localhost:*"]\n',
    "hook 在沙箱外": BOX + HOOK + 'sandbox = "off"\n',
    "hook 放开网络": BOX + HOOK + 'sandbox_network = "allow"\n',
    "hook 的白名单放行": BOX + HOOK + 'network_allow = ["localhost:8765"]\n',
    "MCP 服务器在沙箱外": BOX + MCP + 'sandbox = "off"\n',
    "MCP 服务器放开网络": BOX + MCP + 'sandbox_network = "allow"\n',
    "MCP 服务器的白名单放行": BOX + MCP + 'network_allow = ["127.0.0.1:*"]\n',
}

UNREACHABLE = {
    "默认空白名单": BOX,
    "断网": BOX + 'sandbox_network = "deny"\n',
    "白名单只放行别的本机端口": BOX + 'network_allow = ["localhost:5432"]\n',
    "白名单只放行外部主机": BOX + 'network_allow = ["pypi.org", "*.github.com:*"]\n',
    "hook 与 MCP 跟随全局": BOX + HOOK + MCP,
    "断网时 hook 的白名单不生效": BOX + 'sandbox_network = "deny"\n' + HOOK + 'network_allow = ["localhost:8765"]\n',
}


@pytest.mark.parametrize("text", REACHABLE.values(), ids=REACHABLE.keys())
def test_no_token_refused_when_sandboxed_commands_reach_the_console(serve, config, capsys, text):
    code, app = serve("--no-token", *config(text))
    assert code == 2 and app is None
    err = capsys.readouterr().err
    assert "8765" in err and "批准" in err and "--no-token" in err


@pytest.mark.parametrize("text", UNREACHABLE.values(), ids=UNREACHABLE.keys())
def test_no_token_allowed_when_sandboxed_commands_cannot_reach_it(serve, config, text):
    code, app = serve("--no-token", *config(text))
    assert code == 0 and app is not None


def test_no_token_check_follows_the_actual_port(serve, config):
    allow = BOX + 'network_allow = ["localhost:9000"]\n'
    assert serve("--no-token", "--port", "9000", *config(allow))[0] == 2
    assert serve("--no-token", "--port", "9001", *config(allow))[0] == 0


def test_sandbox_flags_on_the_command_line_count(serve):
    assert serve("--no-token", "--sandbox", "off")[0] == 2
    assert serve("--no-token", "--sandbox", "macos", "--net-allow", "localhost:8765")[0] == 2
    assert serve("--no-token", "--sandbox", "macos", "--network", "allow")[0] == 2
    assert serve("--no-token", "--sandbox", "macos")[0] == 0


def test_a_token_lifts_the_reachability_check(serve):
    code, app = serve("--sandbox", "off")
    assert code == 0 and app is not None


def test_unavailable_sandbox_does_not_count_as_reachable(serve, config, monkeypatch):
    """配置了沙箱但这台机器用不了：命令根本不会执行。"""
    from flywhl_harness.tools import confine
    from flywhl_harness.tools.sandbox import Detected

    monkeypatch.setattr(confine, "detect", lambda: Detected("", "没有可用的系统沙箱"))
    assert serve("--no-token")[0] == 0
    assert serve("--no-token", *config(HOOK + 'sandbox_network = "allow"\n'))[0] == 0, "这个 hook 同样不会执行"
    assert serve("--no-token", *config(HOOK + 'sandbox = "off"\n'))[0] == 2, "写了 off 的 hook 不受影响，照样在沙箱外运行"


# ---------------------------------------------------------------------- 判定函数本身
@pytest.mark.parametrize("host", ["0.0.0.0", "::", "::0", "0:0:0:0:0:0:0:0", ""])
def test_wildcard_spellings(host):
    assert is_wildcard(host)
    assert console_hosts(host, ["box.lan"]) == ["box.lan"]
    assert console_url(host, PORT, None) in (f"http://127.0.0.1:{PORT}/", f"http://[::1]:{PORT}/")


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "192.168.1.20", "fd00::1", "box.lan"])
def test_specific_hosts_are_not_wildcards(host):
    assert not is_wildcard(host)


def test_console_hosts_and_url():
    assert console_hosts("127.0.0.1", []) == []
    assert console_hosts("192.168.1.20", ["box.lan"]) == ["box.lan", "192.168.1.20"]
    assert console_url("::1", PORT, "abc") == f"http://[::1]:{PORT}/?token=abc"


@pytest.mark.parametrize(("host", "kw", "needle"), [
    ("0.0.0.0", {}, "--expose"),
    ("127.0.0.2", {}, "--expose"),                                  # 判定依据是 LOOPBACK_HOSTS，不是地址段
    ("0.0.0.0", {"expose": True, "no_token": True}, "回环地址"),
    ("0.0.0.0", {"expose": True, "token": "x" * 15}, "16"),
    ("0.0.0.0", {"expose": True, "token": ""}, "16"),
    ("127.0.0.1", {"allow_hosts": ["*"]}, "--allow-host"),
])
def test_serve_refusal(host, kw, needle):
    args = {"expose": False, "no_token": False, "token": None, "allow_hosts": [], **kw}
    assert needle in serve_refusal(host, **args)


@pytest.mark.parametrize(("host", "kw"), [
    ("127.0.0.1", {}),
    ("localhost", {"no_token": True}),
    ("::1", {"token": "short"}),
    ("127.0.0.1", {"expose": True}),
    ("0.0.0.0", {"expose": True}),                                  # 令牌用默认生成的
    ("0.0.0.0", {"expose": True, "token": "x" * 16, "allow_hosts": ["box.lan"]}),
])
def test_serve_refusal_lets_valid_arguments_through(host, kw):
    assert serve_refusal(host, **{"expose": False, "no_token": False, "token": None, "allow_hosts": [], **kw}) is None


LOOPBACK_RULES = [
    ("localhost:8765", True), ("127.0.0.1:8765", True), ("localhost:*", True), ("127.0.0.1:*", True),
    ("LOCALHOST:8765", True),
    ("0.0.0.0:8765", True),            # 连 0.0.0.0 落到本机，代理接受写全 IP 的规则
    ("0.0.0.0:*", True),
    ("localhost", False),              # 没写端口只放行 80、443
    ("localhost:5432", False), ("127.0.0.1:5432", False), ("0.0.0.0:5432", False),
    ("*.localhost:8765", False),       # 通配不含 localhost 本身
    ("*.0.0.1:8765", False),           # 字符串上匹配 127.0.0.1，但通配规则进不了回环地址
    ("pypi.org", False), ("registry.local:8765", False), ("192.168.1.20:8765", False),
]


@pytest.mark.parametrize(("rule", "expected"), LOOPBACK_RULES)
async def test_loopback_verdict_agrees_with_the_real_proxy(rule, expected):
    """判定和代理走同一份规则对象，这里再对一遍代理自己的结论：两边不一致就是有路能绕过去。"""
    async def resolve(host, port):
        return [ipaddress.ip_address(a) for a in {"localhost": ("127.0.0.1", "::1")}.get(host, (host,))]

    proxy = EgressProxy(parse_rules([rule]), resolve=resolve)

    async def passes(name: str) -> bool:
        if not proxy.allowed(name, PORT):
            return False
        try:
            return bool(await proxy.vet(name, PORT))
        except EgressDenied:
            return False

    verdicts = [await passes(name) for name in ("127.0.0.1", "localhost", "0.0.0.0")]
    assert any(verdicts) is expected
    assert rules_reach_loopback(proxy.rules, PORT) is expected


def test_loopback_verdict_for_default_ports():
    assert rules_reach_loopback(parse_rules(["localhost"]), 443)
    assert rules_reach_loopback(parse_rules(["pypi.org", "127.0.0.1:9000"]), 9000)
    assert not rules_reach_loopback([], PORT)


SANDBOXED = {"level": "macos", "network": "allowlist"}


@pytest.mark.parametrize(("box", "reached"), [
    (Confinement(), True),                                                        # 档位 off
    (Confinement.resolve("off"), True),
    (Confinement("macos", "allow"), True),
    (Confinement("macos", "allowlist", parse_rules(["localhost:8765"])), True),
    (Confinement("macos", "allowlist"), False),
    (Confinement("macos", "allowlist", parse_rules(["localhost:5432"])), False),
    (Confinement("macos", "deny", parse_rules(["localhost:8765"])), False),       # 规则只在 allowlist 下生效
    (Confinement("linux", "deny"), False),
    (Confinement(network="allowlist", problem="没有可用的系统沙箱"), False),
    (Confinement(network="allow", problem="没有可用的系统沙箱"), False),
])
def test_console_reachers_global(box, reached):
    assert bool(console_reachers(box, PORT)) is reached


@pytest.mark.parametrize(("global_box", "own", "reached"), [
    (SANDBOXED, {}, False),                                                       # 跟随全局
    (SANDBOXED, {"sandbox": "off"}, True),
    (SANDBOXED, {"sandbox_network": "allow"}, True),
    (SANDBOXED, {"network_allow": ["localhost:8765"]}, True),
    (SANDBOXED, {"network_allow": ["0.0.0.0:8765"]}, True),
    (SANDBOXED, {"network_allow": ["localhost:5432"]}, False),
    ({"level": "macos", "network": "deny"}, {"network_allow": ["localhost:8765"]}, False),
    ({"level": "macos", "network": "deny"}, {"sandbox_network": "allowlist", "network_allow": ["localhost:8765"]}, True),
    ({"level": "macos", "network": "deny"}, {"sandbox": "off"}, True),
    # 全局用不了沙箱：跟着全局的不会执行，自己写了 off 的照样在沙箱外运行
    ({"network": "allowlist", "problem": "没有沙箱"}, {"sandbox_network": "allow"}, False),
    ({"network": "allowlist", "problem": "没有沙箱"}, {"sandbox": "off"}, True),
])
@pytest.mark.parametrize("kind", ["hook", "mcp"])
def test_console_reachers_per_entry(kind, global_box, own, reached):
    box = Confinement(**global_box)
    if kind == "hook":
        found = console_reachers(box, PORT, hooks=[HookConfig(event="stop", command="exit 0", **own)])
        assert all("hook 0（stop）" in f for f in found)
    else:
        found = console_reachers(box, PORT, mcp_servers={"demo": {"command": "true", **own}})
        assert all("MCP 服务器 demo" in f for f in found)
    assert bool(found) is reached


def test_entry_network_allow_replaces_the_global_rules():
    box = Confinement("macos", "allowlist", parse_rules(["localhost:8765"]))
    found = console_reachers(box, PORT, hooks=[HookConfig(command="exit 0", network_allow=[])])
    assert [f.split("：")[0] for f in found] == ["bash 与验收命令"], "这个 hook 的白名单是空的，只有全局通道连得到"


def test_refusal_names_every_channel():
    found = console_reachers(Confinement("macos", "allowlist"), PORT, [HookConfig(command="exit 0", sandbox="off")],
                             {"demo": {"command": "true", "sandbox_network": "allow"}})
    text = no_token_refusal(PORT, found)
    assert "hook 0" in text and "MCP 服务器 demo" in text and "批准" in text and "去掉 --no-token" in text


@pytest.mark.parametrize(("token", "expected"), [(None, 409), (TOKEN, 401)])
async def test_why_no_token_needs_the_check(ws, token, expected):
    """这条检查的前提：没有令牌时，不带 ``Origin`` 的请求一路走到审批处理函数（409 是审批号不存在）。"""
    from flywhl_harness.server.app import create_app

    app = create_app(make_harness(ws, ["好"]), auto_recover=False, token=token)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://127.0.0.1:{PORT}") as c:
        assert (await c.post("/api/approvals/none", json={"approved": True})).status_code == expected
