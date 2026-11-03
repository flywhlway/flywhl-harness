#!/usr/bin/env python3
"""在操作系统沙箱里运行 pytest：测试进程和它启动的所有子进程，只能写一个临时目录，也读不到真实家目录。

为什么要有它：2026-10-31，一条回归测试在旧代码上真的执行了 ``rm -rf ~``（复盘见
docs/quality-audit/incident-2026-10-31.md）。HOME 隔离、无害载荷都是约定，约定会被忘记；这里把“测试碰不到开发机”交给内核。

用法与 pytest 相同：``python scripts/safe_pytest.py -q``。直接运行 ``pytest`` 时，根目录的 conftest.py 会自动转到这里。

- **macOS**：sandbox-exec（Seatbelt）。只有临时目录可写；真实家目录里除了仓库本身和 Python 安装目录，文件内容一律不可读。
- **Linux**：有 bubblewrap 就用（根目录只读、家目录换成空目录、只有临时目录可写）；没有就只隔离环境变量，并打印警告。
- **两段运行**：系统沙箱不能嵌套，外层已有沙箱时，内层的 sandbox-exec 会被拒绝。所以：
  1. 外层沙箱里跑全部用例，标记了 ``os_sandbox`` 的除外；
  2. ``os_sandbox`` 用例测的就是 bash 工具的系统沙箱档位，它们执行的命令本身已经在沙箱里。第二段在外层沙箱之外运行，
     而且只在被测代码就是本仓库时运行；拿这个脚本去跑别处导出的旧代码，第二段直接跳过。
- 两段都把 HOME、TMPDIR、XDG_* 指向临时目录；根目录 conftest.py 里还有执行绊线，在子进程启动前拦下灾难性命令。
- **覆盖率**：设 ``FLYWHL_COVERAGE=1`` 时两段都在 coverage 下运行。第一段的沙箱里仓库只读，数据先写进临时目录，
  两段跑完再合并成当前目录的 ``.coverage``，之后用 ``coverage report`` / ``coverage xml`` 出报告。

调试时可以设 ``FLYWHL_TEST_SANDBOX=off`` 直接运行 pytest，不经过本入口（仍有 HOME 隔离和执行绊线）。
"""

from __future__ import annotations

import os
import pwd
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MARK = "os_sandbox"
FLAG = "FLYWHL_TEST_SANDBOX"
COVERAGE = "FLYWHL_COVERAGE"
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
NO_TESTS = 5   # pytest 的退出码：没有收集到用例
_DEVICES = ('(literal "/dev/null")', '(literal "/dev/zero")', '(literal "/dev/dtracehelper")',
            '(regex #"^/dev/fd/")', '(regex #"^/dev/tty")')


def real_home() -> str:
    return os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)


def _q(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def split_marker(args: list[str]) -> tuple[list[str], str | None]:
    """取出 ``-m 表达式``：两段要在它的基础上各加一个条件。"""
    rest, expr, i = [], None, 0
    while i < len(args):
        a = args[i]
        if a == "-m" and i + 1 < len(args):
            expr, i = args[i + 1], i + 2
            continue
        if a.startswith("-m") and not a.startswith("--") and len(a) > 2:
            expr, i = a[2:].removeprefix("="), i + 1
            continue
        rest.append(a)
        i += 1
    return rest, expr


def default_marker(cwd: Path) -> str | None:
    """命令行没给 ``-m`` 时沿用 pyproject 的 addopts（本仓库是 ``not live``）：两段各自追加的 ``-m`` 会覆盖它。"""
    try:
        import tomllib

        opts = tomllib.loads((cwd / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]
        return split_marker(shlex.split(opts.get("addopts", "")))[1]
    except (OSError, KeyError, ValueError, ImportError):
        return None


def phase_marker(expr: str | None, *, inside: bool) -> str:
    cond = f"not {MARK}" if inside else MARK
    return f"({expr}) and {cond}" if expr else cond


def _readable(root: Path, cwd: Path) -> list[str]:
    """真实家目录里仍然可读的位置：仓库、Python 安装目录（uv 装的解释器在家目录下）、本次的临时根。"""
    return sorted({os.path.realpath(p) for p in (cwd, REPO, sys.base_prefix, sys.prefix, sys.exec_prefix, root)})


def _within(path: str, base: str) -> bool:
    return path == base or path.startswith(base + os.sep)


def test_path(root: Path, cwd: Path) -> str:
    """当前解释器的目录排在最前，测试里的 python3 就是跑 pytest 的这个；家目录里读不到的 PATH 条目
    （pyenv shims、nvm……）去掉，否则命令会以“权限不足”这种误导性的方式失败。"""
    home, keep = real_home(), _readable(root, cwd)
    dirs = [os.path.dirname(sys.executable), *os.environ.get("PATH", "").split(os.pathsep)]
    usable = [d for d in dirs if d and (not _within(os.path.realpath(d), home)
                                        or any(_within(os.path.realpath(d), k) for k in keep))]
    return os.pathsep.join(dict.fromkeys(usable))


def isolated_env(root: Path, cwd: Path, mode: str) -> dict[str, str]:
    env = dict(os.environ)
    dirs = {"HOME": "home", "TMPDIR": "tmp", "XDG_CONFIG_HOME": "xdg/config", "XDG_CACHE_HOME": "xdg/cache",
            "XDG_DATA_HOME": "xdg/data", "XDG_STATE_HOME": "xdg/state", "FLYWHL_HOME": "flywhl-home"}
    for name, sub in dirs.items():
        (root / sub).mkdir(parents=True, exist_ok=True)
        env[name] = str(root / sub)
    env[FLAG] = mode
    env["PATH"] = test_path(root, cwd)
    env["PYTHONDONTWRITEBYTECODE"] = "1"   # 仓库在沙箱里只读：不写 __pycache__，也不写 .pytest_cache
    env["PYTEST_ADDOPTS"] = (env.get("PYTEST_ADDOPTS", "") + " -p no:cacheprovider").strip()
    if os.environ.get(COVERAGE) == "1":
        (root / "coverage").mkdir(exist_ok=True)
        env["COVERAGE_FILE"] = str(root / "coverage" / ".coverage")   # pyproject 设了 parallel，每个进程各写一份
    return env


def seatbelt_profile(root: Path, cwd: Path) -> str:
    keep = " ".join(f"(subpath {_q(p)})" for p in _readable(root, cwd))
    writable = " ".join([f"(subpath {_q(os.path.realpath(root))})", *_DEVICES])
    return "\n".join([
        "(version 1)",
        "(allow default)",
        f"(deny file-write* (require-not (require-any {writable})))",
        # 只拦文件内容与目录列表，不拦 stat：路径解析、realpath 要逐级 lstat 家目录
        f"(deny file-read-data (require-all (subpath {_q(real_home())}) (require-not (require-any {keep}))))",
    ])


def bwrap_argv(root: Path, cwd: Path) -> list[str]:
    home = real_home()
    # 新的 PID 命名空间与 /proc：测试进程看不到开发者其它进程的 /proc/<pid>/environ
    argv = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-pid", "--tmpfs", "/tmp"]
    if os.path.isdir(home):
        argv += ["--tmpfs", home]   # 家目录换成空目录；仓库与 Python 在家目录下时，下面再只读挂回来
    for p in _readable(root, cwd):
        if p.startswith(home + os.sep) and p != os.path.realpath(root):
            argv += ["--ro-bind", p, p]
    return [*argv, "--bind", str(root), str(root), "--die-with-parent", "--chdir", str(cwd)]


def _bwrap_works(argv: list[str]) -> bool:
    """用实际要用的参数试跑一次：Docker 默认遮住 /proc 的部分路径，只试 --ro-bind 会通过，挂新 /proc 时才失败。"""
    try:
        return subprocess.run([*argv, "true"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def outer_sandbox(root: Path, cwd: Path) -> tuple[str, list[str]]:
    if sys.platform == "darwin" and os.path.exists(SANDBOX_EXEC):
        return "seatbelt", [SANDBOX_EXEC, "-p", seatbelt_profile(root, cwd)]
    if sys.platform.startswith("linux") and shutil.which("bwrap") and _bwrap_works(argv := bwrap_argv(root, cwd)):
        return "bwrap", argv
    return "env-only", []


def combine(codes: list[int]) -> int:
    """某一段没选中用例（退出码 5）不算失败；两段都没选中才返回 5。"""
    real = [c for c in codes if c != NO_TESTS]
    if not real:
        return NO_TESTS
    return next((c for c in real if c != 0), 0)


def pytest_command(args: list[str], *, cov: bool) -> list[str]:
    return [sys.executable, *(["-m", "coverage", "run"] if cov else []), "-m", "pytest", *args]


def combine_coverage(root: Path, cwd: Path) -> None:
    """两段（以及被测到的子进程）各写一份数据，合并到当前目录；临时目录随后就删。"""
    parts = root / "coverage"
    if not any(parts.glob(".coverage.*")):
        print("警告：没有收集到覆盖率数据", file=sys.stderr)
        return
    subprocess.call([sys.executable, "-m", "coverage", "combine", "-q", "--data-file", str(cwd / ".coverage"), str(parts)])


def cleanup(root: Path) -> None:
    if os.environ.get("FLYWHL_KEEP_TEST_ROOT"):
        print(f"保留测试临时目录：{root}", file=sys.stderr)
        return
    target, base = os.path.realpath(root), os.path.realpath(tempfile.gettempdir())
    # 删除前核对目标：只删本次 mkdtemp 建出来的目录
    if os.path.dirname(target) == base and os.path.basename(target).startswith("flywhl-test-"):
        shutil.rmtree(target, ignore_errors=True)


def main(argv: list[str]) -> int:
    cwd = Path.cwd()
    args, expr = split_marker(argv)
    if expr is None:
        expr = default_marker(cwd)
    cov = os.environ.get(COVERAGE) == "1"
    pytest_cmd = pytest_command(args, cov=cov)
    root = Path(tempfile.mkdtemp(prefix="flywhl-test-"))
    try:
        mode, wrap = outer_sandbox(root, cwd)
        if not wrap:
            print("警告：没有可用的操作系统沙箱（macOS sandbox-exec / Linux bubblewrap），只隔离了 HOME、TMPDIR 等环境变量",
                  file=sys.stderr, flush=True)
            codes = [subprocess.call([*pytest_cmd, *(["-m", expr] if expr else [])], env=isolated_env(root, cwd, mode))]
        else:
            print(f"== 第一段：{mode} 外层沙箱，只能写 {root}", flush=True)
            codes = [subprocess.call([*wrap, *pytest_cmd, "-m", phase_marker(expr, inside=True)],
                                     env=isolated_env(root, cwd, mode))]
            if cwd.resolve() != REPO:
                print(f"== 第二段跳过：被测代码不是本仓库，{MARK} 用例不在外层沙箱之外运行", flush=True)
            else:
                print(f"== 第二段：{MARK} 用例在外层沙箱之外运行（它们执行的命令本身在系统沙箱里）", flush=True)
                codes.append(subprocess.call([*pytest_cmd, "-m", phase_marker(expr, inside=False)],
                                             env=isolated_env(root, cwd, "outside")))
        if cov:
            combine_coverage(root, cwd)
        return combine(codes)
    finally:
        cleanup(root)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
