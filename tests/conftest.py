"""测试公共设施：在临时工作区里用脚本化模型构造 Harness。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from flywhl_harness import Harness, HarnessConfig, ScriptedProvider, Store
from flywhl_harness.events import E
from flywhl_harness.llm.base import Provider


def make_harness(ws: Path, script: list[Any] | Provider, *, store: Store | None = None, on_ask: str = "allow",
                 **cfg_over: Any) -> Harness:
    cfg = HarnessConfig.load(workspace=str(ws))
    cfg.permissions.on_ask = on_ask
    cfg.snapshots = cfg_over.pop("snapshots", False)
    cfg.heartbeat_seconds = 0.2
    for k, v in cfg_over.items():
        target = cfg
        *path, last = k.split("__")
        for p in path:
            target = getattr(target, p)
        setattr(target, last, v)
    provider = script if isinstance(script, Provider) else ScriptedProvider(script)
    return Harness(cfg, provider=provider, model="scripted", store=store or Store(":memory:"))


def types_of(events, *skip: str) -> list[str]:
    skip_set = set(skip) | {E.MESSAGE_UPDATE}
    return [e.type for e in events if e.type not in skip_set]


def tool_ends(events) -> list[dict]:
    return [e.data for e in events if e.type == E.TOOL_END]


@pytest.fixture(autouse=True)
def _isolated_flywhl_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """用户配置与信任记录指向临时目录：本机的 ~/.flywhl 不影响测试，测试也不会写它。
    HOME 同样换成临时目录：测试会真正执行 bash 命令，防线一旦回归，命令里的 ~ 不能落到开发者的家目录上。"""
    monkeypatch.setenv("FLYWHL_HOME", str(tmp_path / "flywhl-home"))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    d = tmp_path / "ws"
    d.mkdir()
    return d
