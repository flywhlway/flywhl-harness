"""测试设施自身的防线：系统沙箱入口、执行绊线、HOME 隔离（见仓库根目录 conftest.py 与 scripts/safe_pytest.py）。

这里的探针都写成 ``false && …``：绊线一旦失效，命令照样在 ``false`` 处短路，不会真的删除任何东西。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

import conftest as repo_conftest

ROOT = Path(__file__).resolve().parent.parent


def _runner():
    spec = importlib.util.spec_from_file_location("safe_pytest", ROOT / "scripts" / "safe_pytest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tripwire_blocks_catastrophic_and_real_home_deletes():
    start = len(repo_conftest._tripped)
    probe = os.path.join(repo_conftest.REAL_HOME, "flywhl-tripwire-probe")
    for cmd in ("false && rm -rf ~", f"false && rm -rf {probe}", f"false && find {probe} -delete"):
        with pytest.raises(PermissionError, match="执行绊线"):
            subprocess.run(["/bin/bash", "-c", cmd])
    with pytest.raises(PermissionError, match="执行绊线"):
        subprocess.run(f"false && rm -rf {probe}", shell=True)
    with pytest.raises(PermissionError, match="执行绊线"):
        shutil.rmtree(probe)
    assert len(repo_conftest._tripped) == start + 5
    del repo_conftest._tripped[start:]   # 有意触发的，不算这个测试违规


def test_tripwire_lets_ordinary_commands_through(tmp_path):
    (tmp_path / "x").write_text("1")
    r = subprocess.run(["/bin/bash", "-c", f"rm -f {tmp_path / 'x'} && echo done"], capture_output=True, text=True)
    assert r.stdout.strip() == "done" and not (tmp_path / "x").exists()
    d = tmp_path / "d"
    d.mkdir()
    shutil.rmtree(d)
    assert not d.exists()


def test_each_test_gets_an_isolated_home_and_cwd(tmp_path):
    assert os.environ["HOME"] == str(tmp_path / "home")
    assert Path.cwd() == tmp_path
    assert os.path.realpath(os.path.expanduser("~")) != repo_conftest.REAL_HOME


@pytest.mark.skipif(os.environ.get("FLYWHL_TEST_SANDBOX") not in ("seatbelt", "bwrap"), reason="只在外层系统沙箱里检查")
def test_outer_sandbox_hides_the_real_home():
    """只有内核规则真的生效时，进程才会看不到家目录里的东西：环境变量说在沙箱里不算数。

    seatbelt 直接拒绝读取；bwrap 把家目录换成空的 tmpfs，只把仓库与 Python 所在的路径挂回来。
    """
    home = repo_conftest.REAL_HOME
    try:
        names = set(os.listdir(home))
    except OSError:
        names = set()
    kept = {Path(p).relative_to(home).parts[0] for p in _runner()._readable(ROOT, ROOT) if p.startswith(home + os.sep)}
    assert names <= kept, f"沙箱里还看得见家目录下的 {sorted(names - kept)}"
    assert (ROOT / "pyproject.toml").read_text(encoding="utf-8")   # 仓库照常可读


def test_runner_splits_the_marker_expression():
    r = _runner()
    assert r.split_marker(["-q", "-m", "live", "tests/live"]) == (["-q", "tests/live"], "live")
    assert r.split_marker(["-mlive", "-x"]) == (["-x"], "live")
    assert r.split_marker(["--maxfail=1"]) == (["--maxfail=1"], None)
    assert r.phase_marker("not live", inside=True) == "(not live) and not os_sandbox"
    assert r.phase_marker(None, inside=False) == "os_sandbox"
    assert r.default_marker(ROOT) == "not live"


def test_runner_exit_codes_and_cleanup_target(monkeypatch):
    r = _runner()
    assert r.combine([0, 5]) == 0 and r.combine([5, 5]) == 5 and r.combine([1, 0]) == 1 and r.combine([5, 2]) == 2
    # 只删本次 mkdtemp 建出来的 flywhl-test-* 目录：名字或位置不对就不动
    base = Path(tempfile.mkdtemp())
    other = base / "keep-me"
    other.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(base))
    r.cleanup(other)
    assert other.exists()
    mine = base / "flywhl-test-abc"
    mine.mkdir()
    r.cleanup(mine)
    assert not mine.exists()


def test_runner_collects_coverage_in_the_writable_root(tmp_path, monkeypatch):
    r = _runner()
    monkeypatch.delenv("COVERAGE_FILE", raising=False)
    monkeypatch.delenv(r.COVERAGE, raising=False)
    assert "COVERAGE_FILE" not in r.isolated_env(tmp_path, ROOT, "seatbelt")
    monkeypatch.setenv(r.COVERAGE, "1")
    # 第一段的沙箱里只有临时目录可写，数据先落在那里，两段跑完再合并出来
    assert r.isolated_env(tmp_path, ROOT, "seatbelt")["COVERAGE_FILE"] == str(tmp_path / "coverage" / ".coverage")
    py = r.sys.executable
    assert r.pytest_command(["-q"], cov=False) == [py, "-m", "pytest", "-q"]
    assert r.pytest_command(["-q"], cov=True) == [py, "-m", "coverage", "run", "-m", "pytest", "-q"]


def test_runner_path_drops_unreadable_home_entries(tmp_path):
    r = _runner()
    path = r.test_path(tmp_path, ROOT).split(os.pathsep)
    assert path[0] == os.path.dirname(os.path.realpath(r.sys.executable)) or path[0] == os.path.dirname(r.sys.executable)
    home = repo_conftest.REAL_HOME
    keep = r._readable(tmp_path, ROOT)
    for d in path:
        rd = os.path.realpath(d)
        if rd == home or rd.startswith(home + os.sep):
            assert any(rd == k or rd.startswith(k + os.sep) for k in keep), d
