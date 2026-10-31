"""能力清单（docs/quality-audit/capability-backlog.md）各项的回归测试。

执行型用例的载荷一律是无害哨兵；仓库根目录的 conftest.py 另有系统沙箱与执行绊线兜底。
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

from flywhl_harness.config import HarnessConfig, HookConfig

from .conftest import make_harness, tool_ends

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
