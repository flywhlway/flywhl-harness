"""仓库级 pytest 设施：测试碰不到开发机。tests/ 与 docs/quality-audit/tests 都经过这里。

1. **系统沙箱**：不是经 ``scripts/safe_pytest.py`` 启动的，就转到它那里，在操作系统沙箱里重新运行；
2. **HOME 隔离**：每个测试的 HOME、FLYWHL_HOME 指向临时目录，命令里的 ``~`` 落不到开发者的家目录上；
3. **执行绊线**：子进程启动前检查命令，灾难性命令、对真实家目录的破坏性操作一律拒绝，并让当前测试失败；
   ``shutil.rmtree`` 删真实家目录下的目录同样拒绝。被测代码的防线一旦回归，载荷停在这里，不会真的执行。

三层各管一段：沙箱兜住绊线认不出的写法；绊线兜住沙箱不在的场合（第二段、没有 bubblewrap 的 Linux、``FLYWHL_TEST_SANDBOX=off``）。
"""

from __future__ import annotations

import os
import pwd
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
FLAG = "FLYWHL_TEST_SANDBOX"
REAL_HOME = os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
_DELETERS = {"rm", "rmdir", "unlink", "shred", "srm", "mv", "chmod", "chown", "chflags", "dd", "truncate", "rsync"}
_tripped: list[str] = []
_busy = threading.local()


def pytest_configure(config: pytest.Config) -> None:
    if os.environ.get(FLAG):
        return
    capman = config.pluginmanager.getplugin("capturemanager")
    if capman is not None:   # 全局输出捕获已经接管了 fd 1/2，exec 之前要还回去，否则新进程的输出会写进捕获文件
        capman.stop_global_capturing()
    os.execv(sys.executable, [sys.executable, str(ROOT / "scripts" / "safe_pytest.py"), *config.invocation_params.args])


def pytest_report_header(config: pytest.Config) -> str:
    notes = {"seatbelt": "macOS sandbox-exec，只能写临时目录", "bwrap": "bubblewrap，只能写临时目录",
             "outside": "外层沙箱之外，只运行 os_sandbox 用例（命令本身在系统沙箱里执行）",
             "env-only": "没有可用的系统沙箱，只隔离了环境变量", "off": "已关闭，只有 HOME 隔离与执行绊线"}
    mode = os.environ.get(FLAG, "")
    return f"测试沙箱：{notes.get(mode, mode)}"


@pytest.fixture(autouse=True)
def _no_nested_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """系统沙箱不能嵌套：默认配置的 ``auto`` 在测试里落到 off，测沙箱的用例显式写档位。一次性容器里的回归设
    ``FLYWHL_TEST_REAL_SANDBOX=1``，按真实默认值运行。"""
    if os.environ.get("FLYWHL_TEST_REAL_SANDBOX"):
        return
    try:
        from flywhl_harness.tools import confine
        from flywhl_harness.tools.sandbox import Detected
    except ImportError:   # 被测代码是旧版本
        return
    monkeypatch.setattr(confine, "detect", lambda: Detected("off"))


# ---------------------------------------------------------------------- 执行绊线
def _under_home(word: str) -> bool:
    if not word.startswith("/"):
        return False
    p = os.path.realpath(word)
    return p == REAL_HOME or p.startswith(REAL_HOME + os.sep)


def _payloads(args, shell: bool) -> list[str]:
    if isinstance(args, (str, bytes, os.PathLike)):
        return [os.fsdecode(args)]
    argv = [os.fsdecode(a) for a in args]
    inner = [argv[i + 1] for i in range(1, len(argv) - 1)   # sandbox-exec / bwrap 包着的 bash -c 也要看到里面那条命令
             if argv[i] in ("-c", "-lc", "-ic", "-ec") and os.path.basename(argv[i - 1]) in _SHELLS]
    return inner or [shlex.join(argv)]


def _danger(cmd: str) -> str | None:
    try:
        from flywhl_harness.policy.bash_analyzer import analyze

        if analyze(cmd).catastrophic:
            return "灾难性命令"
    except Exception:   # 被测代码是旧版本或者坏了：只靠下面这条
        pass
    try:
        words = shlex.split(cmd, comments=True)
    except ValueError:
        words = cmd.split()
    names = {os.path.basename(w) for w in words}
    deleting = names & _DELETERS or ("find" in names and ("-delete" in words or "-exec" in words))
    if deleting and any(_under_home(w) for w in words):
        return "对真实家目录的破坏性操作"
    return None


_popen_init = subprocess.Popen.__init__


def _guarded_popen_init(self, args, *a, **kw):
    if not getattr(_busy, "on", False):   # 分析器自己若起子进程，不再重复检查
        _busy.on = True
        try:
            hits = [(why, cmd) for cmd in _payloads(args, kw.get("shell", False)) if (why := _danger(cmd))]
        finally:
            _busy.on = False
        if hits:
            why, cmd = hits[0]
            _tripped.append(f"{why}：{cmd[:300]}")
            raise PermissionError(f"测试执行绊线拦下了{why}：{cmd[:120]}")
    _popen_init(self, args, *a, **kw)


_rmtree = shutil.rmtree


def _guarded_rmtree(path, *a, **kw):
    if isinstance(path, (str, bytes, os.PathLike)):
        p = os.path.realpath(os.fsdecode(path))
        if p == "/" or p == REAL_HOME or p.startswith(REAL_HOME + os.sep):
            _tripped.append(f"shutil.rmtree：{p}")
            raise PermissionError(f"测试执行绊线拦下了删除：{p}")
    return _rmtree(path, *a, **kw)


_guarded_rmtree.avoids_symlink_attacks = _rmtree.avoids_symlink_attacks
subprocess.Popen.__init__ = _guarded_popen_init
shutil.rmtree = _guarded_rmtree


@pytest.fixture(autouse=True)
def _tripwire_check():
    """被测代码可能吞掉 PermissionError：绊线响过就让这个测试失败。"""
    start = len(_tripped)
    yield
    if len(_tripped) > start:
        pytest.fail("测试执行绊线：\n" + "\n".join(_tripped[start:]), pytrace=False)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """用户配置与信任记录指向临时目录：本机的 ~/.flywhl 不影响测试，测试也不会写它。
    HOME 同样换成临时目录：测试会真正执行 bash 命令，防线一旦回归，命令里的 ~ 不能落到开发者的家目录上。
    当前目录也换过去：``HarnessConfig()`` 的工作区默认是 "."，否则演示工具的数据库会写进仓库的 .flywhl/。"""
    monkeypatch.setenv("FLYWHL_HOME", str(tmp_path / "flywhl-home"))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
