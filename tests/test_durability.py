"""会话库的落盘强度：配置怎么传到 Store，SQLite 连接上实际生效的是什么。"""

from __future__ import annotations

import ast
import warnings
from pathlib import Path

import pytest

from flywhl_harness import Harness, HarnessConfig, ScriptedProvider, Store
from flywhl_harness.config import _SAFE_KEYS, UntrustedConfigWarning, split_untrusted
from flywhl_harness.observe import replay
from flywhl_harness.observe.replay import replay_run

from .conftest import make_harness

SRC = Path(__file__).resolve().parents[1] / "src"


def pragmas(store: Store) -> dict[str, int]:
    """从连接上读回实际值。synchronous：1 = NORMAL，2 = FULL。"""
    return {p: store._q(f"PRAGMA {p}")[0][0] for p in ("synchronous", "fullfsync", "checkpoint_fullfsync")}


FULL = {"synchronous": 2, "fullfsync": 1, "checkpoint_fullfsync": 1}


def test_store_defaults_to_full(tmp_path):
    s = Store(tmp_path / "h.db")
    assert pragmas(s) == FULL
    assert s._q("PRAGMA journal_mode")[0][0] == "wal"


def test_store_normal(tmp_path):
    p = pragmas(Store(tmp_path / "h.db", durability="normal"))
    assert (p["synchronous"], p["fullfsync"]) == (1, 0)


def test_settings_apply_to_every_connection_not_to_the_file(tmp_path):
    """这些 PRAGMA 不存进库文件：normal 建的库用默认设置重新打开，仍然要是 full。"""
    db = tmp_path / "h.db"
    Store(db, durability="normal").close()
    assert pragmas(Store(db)) == FULL


@pytest.mark.parametrize("bad", ["FULL", "fast", "", None, 1])
def test_store_rejects_unknown_durability(tmp_path, bad):
    with pytest.raises(ValueError, match="durability"):
        Store(tmp_path / "h.db", durability=bad)


def test_harness_builds_its_store_from_config(ws):
    """不传 store 时走 Harness 自己建库的路径：这是 CLI 和 Web 服务的实际路径。"""
    default = Harness(HarnessConfig.load(workspace=str(ws)), provider=ScriptedProvider(["x"]), model="scripted")
    assert pragmas(default.store) == FULL
    default.store.close()
    normal = Harness(HarnessConfig.load(workspace=str(ws), durability="normal"), provider=ScriptedProvider(["x"]), model="scripted")
    assert pragmas(normal.store)["synchronous"] == 1
    assert Path(normal.store.db_path).parent == normal.data_dir


async def test_replay_store_follows_config(ws, monkeypatch):
    seen: list[Store] = []

    class Spy(Store):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            seen.append(self)

    h = make_harness(ws, ["完成"])
    async with h:
        s = h.create_session("回放")
        run = await h.start_run(s.id, "开始")
        await h.wait(run.id)
    monkeypatch.setattr(replay, "Store", Spy)
    cfg = HarnessConfig.load(workspace=str(ws), durability="normal")
    await replay_run(h.store, run.id, config=cfg, base_tools=h.tools)
    assert [x.durability for x in seen] == ["normal"]


def test_every_store_in_src_is_told_the_durability():
    """新增的构造点漏传时，这里失败，不会悄悄落回 Store 的默认值。"""
    missing = []
    for f in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Store"
                    and not any(k.arg == "durability" for k in node.keywords)):
                missing.append(f"{f}:{node.lineno}")
    assert missing == []


def test_config_default_and_override(tmp_path):
    assert HarnessConfig().durability == "full"
    assert HarnessConfig.load(path=tmp_path / "none.toml").durability == "full"
    assert HarnessConfig.load(path=tmp_path / "none.toml", durability="normal").durability == "normal"
    f = tmp_path / "c.toml"
    f.write_text('durability = "normal"\n', encoding="utf-8")
    assert HarnessConfig.load(path=f).durability == "normal"


def test_invalid_value_fails_at_load(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="durability"):
        HarnessConfig(durability="fast")
    with pytest.raises(ValueError, match="durability"):
        HarnessConfig.load(path=tmp_path / "none.toml", durability="fast")
    f = tmp_path / "c.toml"
    f.write_text('durability = "fast"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="durability"):
        HarnessConfig.load(path=f)
    # 启动目录的项目配置：取值写错要直接报错，不要变成一次信任确认，也不要被当成“未信任”悄悄忽略
    (tmp_path / "flywhl.toml").write_text('durability = "fast"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="durability"):
        HarnessConfig.load()


def test_user_config_is_validated(tmp_path, monkeypatch):
    home = tmp_path / "flywhl-home"
    home.mkdir()
    (home / "config.toml").write_text('durability = "fast"\n', encoding="utf-8")
    monkeypatch.setenv("FLYWHL_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="durability"):
        HarnessConfig.load()


def test_project_config_needs_trust_to_relax_durability(tmp_path, monkeypatch):
    assert "durability" not in _SAFE_KEYS
    assert split_untrusted({"durability": "normal"})[1] == ["durability = 'normal'"]
    (tmp_path / "flywhl.toml").write_text('durability = "normal"\n', encoding="utf-8")
    monkeypatch.setenv("FLYWHL_HOME", str(tmp_path / "flywhl-home"))
    monkeypatch.chdir(tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert HarnessConfig.load().durability == "full"
    assert any(issubclass(w.category, UntrustedConfigWarning) and "durability" in str(w.message) for w in caught)
    assert HarnessConfig.load(trust=lambda req: False).durability == "full"
    assert HarnessConfig.load(trust=lambda req: True).durability == "normal"
    # 信任记录绑定文件内容：之后不用再确认
    assert HarnessConfig.load().durability == "normal"
