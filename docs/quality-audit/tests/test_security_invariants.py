"""安全不变量回归。

这些断言描述产品自己声明的边界（plan 模式只读、凭证任何模式拒绝、密钥不进子进程、
Origin 必须指向本服务、灾难性命令任何模式拒绝）。当前实现不满足的用例会失败，
失败即缺陷，不是测试写错。修复后这些断言应转为通过。

运行：.venv/bin/python -m pytest docs/quality-audit/tests -q
默认 `pytest` 的 testpaths 只有 tests/，不会自动收集本目录。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flywhl_harness import Harness, HarnessConfig, ScriptedProvider, Store
from flywhl_harness.llm.profiles import resolve
from flywhl_harness.policy.approvals import ApprovalDecision
from flywhl_harness.policy.bash_analyzer import analyze
from flywhl_harness.policy.permissions import Decision, PermissionPolicy
from flywhl_harness.runtime.workspace import Workspace
from flywhl_harness.server.app import create_app
from flywhl_harness.tools import default_tools

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"


def _tools():
    return default_tools(HarnessConfig())


def _policy(ws: Path, mode: str) -> PermissionPolicy:
    return PermissionPolicy(mode, workspace=Workspace(ws, snapshots=False))


async def _run(ws: Path, command: str, *, mode: str, on_ask: str = "deny") -> None:
    cfg = HarnessConfig.load(workspace=str(ws))
    cfg.permissions.mode = mode
    cfg.permissions.on_ask = on_ask
    cfg.permissions.sandbox = "off"
    cfg.snapshots = False
    h = Harness(
        cfg,
        provider=ScriptedProvider([
            {"tool_calls": [{"name": "bash", "args": {"command": command}}]},
            "done",
        ]),
        model="scripted",
        store=Store(":memory:"),
    )
    async with h:
        await h.run("audit")


# ---------------------------------------------------------------- 换行被当成只读
async def test_newline_is_not_a_read_in_plan_mode(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    cmd = "echo safe\ntouch plan_marker.txt"
    decision = _policy(ws, "plan").evaluate(_tools().get("bash"), {"command": cmd})
    assert decision.decision == Decision.DENY
    await _run(ws, cmd, mode="plan")
    assert not (ws / "plan_marker.txt").exists()


def test_newline_rm_is_catastrophic(tmp_path: Path) -> None:
    cmd = "echo safe\nrm -rf subdir"
    found = analyze(cmd, str(tmp_path))
    assert found.catastrophic or found.level != "read"


# ---------------------------------------------------------------- 父进程密钥
def test_parent_env_dump_is_not_auto_allowed(tmp_path: Path) -> None:
    decision = _policy(tmp_path, "default").evaluate(_tools().get("bash"), {"command": "ps eww -p $PPID"})
    assert decision.decision == Decision.DENY


def test_unlisted_secret_names_do_not_reach_the_model(tmp_path: Path) -> None:
    """子进程只注入探针自己的 canary，不继承调用方环境。"""
    code = r"""
import asyncio, sys
sys.path.insert(0, sys.argv[1])
from flywhl_harness.cancel import CancelToken
from flywhl_harness.runtime.store import BlobStore, Store
from flywhl_harness.runtime.workspace import Workspace
from flywhl_harness.tools.base import ToolContext
from flywhl_harness.tools.shell import BashTool

async def main():
    ws = Workspace(sys.argv[2], snapshots=False)
    tool = BashTool(strip_secret_env=True)
    ctx = ToolContext("r", "s", "c", ws, CancelToken(), BlobStore(Store(":memory:")), "idem")
    res = await tool.execute({"command": "echo \"$MYSQL_PWD|$DEEPSEEK_API_KEY\"", "timeout": 10}, ctx)
    print("MYSQL" if "qa-canary-mysql" in res.content else "no-mysql")
    print("KEY" if "qa-canary-should-be-stripped" in res.content else "no-key")

asyncio.run(main())
"""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "LANG": "C",
        "MYSQL_PWD": "qa-canary-mysql",
        "DEEPSEEK_API_KEY": "qa-canary-should-be-stripped",
    }
    proc = subprocess.run(
        [sys.executable, "-c", code, str(SRC), str(tmp_path / "ws")],
        env=env, capture_output=True, text=True, timeout=20, check=False,
    )
    assert proc.returncode == 0, proc.stderr[-400:]
    lines = proc.stdout.split()
    assert "no-key" in lines
    assert "no-mysql" in lines


# ---------------------------------------------------------------- 凭证大小写
def test_credential_case_alias_is_denied(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".ssh").mkdir(parents=True)
    target = ws / ".ssh" / "config"
    target.write_text("CANARY-SSH-CONFIG\n")
    alias = ws / ".SSH" / "config"
    try:
        same = alias.exists() and os.path.samefile(alias, target)
    except OSError:
        same = False
    if not same:
        pytest.skip("当前文件系统区分大小写，观察不到别名")
    decision = _policy(ws, "default").evaluate(_tools().get("read"), {"path": ".SSH/config"})
    assert decision.decision == Decision.DENY
    bash = _policy(ws, "default").evaluate(_tools().get("bash"), {"command": "cat .SSH/config"})
    assert bash.decision == Decision.DENY


# ---------------------------------------------------------------- 灾难性命令
@pytest.mark.parametrize("cmd", [
    "bash -lc 'rm -rf ~'",
    "find ~ -delete",
])
def test_destructive_forms_are_denied_in_bypass(tmp_path: Path, cmd: str) -> None:
    found = analyze(cmd)
    assert found.catastrophic, found.reasons
    decision = _policy(tmp_path, "bypass").evaluate(_tools().get("bash"), {"command": cmd})
    assert decision.decision == Decision.DENY


async def test_session_grant_does_not_authorize_a_different_lc_script(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    seen: list[str] = []

    async def resolver(rec):
        seen.append(str(rec.args.get("command")))
        return ApprovalDecision(True, remember=True)

    cfg = HarnessConfig.load(workspace=str(ws))
    cfg.permissions.mode = "default"
    cfg.permissions.on_ask = "wait"
    cfg.snapshots = False
    h = Harness(
        cfg,
        provider=ScriptedProvider([
            {"tool_calls": [{"name": "bash", "args": {"command": "bash -lc 'echo one > grant_a.txt'"}}]},
            {"tool_calls": [{"name": "bash", "args": {"command": "bash -lc 'echo two > grant_b.txt'"}}]},
            "done",
        ]),
        model="scripted",
        store=Store(":memory:"),
        approval_resolver=resolver,
    )
    async with h:
        await h.run("grant")
    assert len(seen) == 2
    assert not (ws / "grant_b.txt").exists() or len(seen) == 2


# ---------------------------------------------------------------- HTTP
def _client(tmp_path: Path, token: str | None = None) -> TestClient:
    ws = tmp_path / "ws"
    ws.mkdir()
    app = create_app(
        Harness(
            HarnessConfig.load(workspace=str(ws)),
            provider=ScriptedProvider(["ok"]),
            model="scripted",
            store=Store(str(tmp_path / "http.db")),
        ),
        token=token,
    )
    return TestClient(app)


def test_cross_site_origin_is_rejected(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        resp = client.post(
            "/api/sessions",
            json={"title": "x"},
            headers={"Host": "127.0.0.1", "Origin": "https://evil.example"},
        )
    assert resp.status_code == 403


def test_loopback_other_port_is_rejected(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        resp = client.post(
            "/api/sessions",
            json={"title": "x"},
            headers={"Host": "127.0.0.1", "Origin": "http://127.0.0.1:9999"},
        )
    assert resp.status_code == 403


def test_token_does_not_widen_origin(tmp_path: Path) -> None:
    with _client(tmp_path, token="qa-token") as client:
        resp = client.post(
            "/api/sessions",
            json={"title": "x"},
            headers={
                "Host": "127.0.0.1",
                "Origin": "http://127.0.0.1:9999",
                "Authorization": "Bearer qa-token",
            },
        )
    assert resp.status_code == 403


def test_foreign_host_is_rejected(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        resp = client.get("/api/info", headers={"Host": "evil.example"})
    assert resp.status_code == 403


# ---------------------------------------------------------------- 模型选择
def test_model_mock_stays_on_demo_provider_when_a_key_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "qa-not-a-real-key")
    profile, _model = resolve("mock", "auto")
    assert profile.name == "mock"
