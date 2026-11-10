"""任务集：加载、铺工作区、跑检查脚本，以及校验任务本身是对的。

一个任务是 ``bench/tasks/<id>/`` 下的一个目录::

    task.toml      id、title、category、prompt，可选 git、serve、sandbox_sensitive
    workspace/     固定的工作区夹具，每次试验复制一份
    check.py       确定性的检查脚本：``python -I check.py <工作区>``，退出码 0 表示通过，失败时打印一行原因
    solution/      参考答案：覆盖到工作区上的文件
    solution.sh    参考答案里文件覆盖做不到的步骤（git 操作、写工作区外的文件），在工作区里用 bash 执行

检查脚本和参考答案放在工作区之外，模型运行期间 ``answers_hidden`` 收走任务目录的权限：沙箱不限制读，只放在别处模型搜全盘还是找得到。
``validate`` 证明两件事：夹具原样时检查不通过（题目不是白送的），参考答案能通过（题目是做得到的）。
提示词里的 ``{port}`` 换成 ``serve`` 目录的本机 HTTP 服务端口，``{home}`` 换成这次试验的家目录。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parent
TASKS = ROOT / "tasks"
CHECK_TIMEOUT = 60
CATEGORIES = ("fix", "feature", "refactor", "test", "data", "repo", "environment")
GIT_ENV = {"GIT_AUTHOR_NAME": "bench", "GIT_AUTHOR_EMAIL": "bench@example.invalid", "GIT_COMMITTER_NAME": "bench",
           "GIT_COMMITTER_EMAIL": "bench@example.invalid", "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
           "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    category: str
    prompt: str
    dir: Path
    git: bool = False
    serve: str | None = None
    sandbox_sensitive: bool = False

    @property
    def workspace(self) -> Path:
        return self.dir / "workspace"

    @property
    def check(self) -> Path:
        return self.dir / "check.py"


def load_tasks(root: Path = TASKS) -> list[Task]:
    tasks = []
    for d in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))):
        data = tomllib.loads((d / "task.toml").read_text(encoding="utf-8"))
        if data["id"] != d.name:
            raise ValueError(f"{d.name}：task.toml 的 id 是 {data['id']}")
        if data["category"] not in CATEGORIES:
            raise ValueError(f"{d.name}：未知分类 {data['category']}")
        tasks.append(Task(data["id"], data["title"], data["category"], data["prompt"].strip(), d, bool(data.get("git")),
                          data.get("serve"), bool(data.get("sandbox_sensitive"))))
    return tasks


def _git(ws: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "init.defaultBranch=main", *args], cwd=ws, env={**os.environ, **GIT_ENV}, check=True,
                   capture_output=True, text=True, stdin=subprocess.DEVNULL)


def prepare(task: Task, ws: Path) -> None:
    """铺工作区。``git = true`` 的任务建一个带初始提交的仓库；夹具里的 ``setup.git`` 脚本再把历史铺好（分支、冲突这类）。"""
    shutil.copytree(task.workspace, ws, dirs_exist_ok=True)
    if task.git:
        _git(ws, "init", "-q")
        _git(ws, "add", "-A")
        _git(ws, "commit", "-q", "-m", "初始提交")
        script = task.dir / "setup.git"
        if script.is_file():
            subprocess.run(["bash", str(script)], cwd=ws, env={**os.environ, **GIT_ENV}, check=True, capture_output=True,
                           text=True, stdin=subprocess.DEVNULL)


@contextmanager
def answers_hidden(root: Path = TASKS) -> Iterator[None]:
    """模型运行期间收走任务目录的权限。拦得住搜全盘，拦不住关沙箱时有意 chmod 回来。

    只在一次性容器里生效：在宿主上跑到一半被杀，目录会留成 000。
    """
    if not os.environ.get("FLYWHL_BENCH_DISPOSABLE"):
        yield
        return
    mode = root.stat().st_mode & 0o777
    root.chmod(0)
    try:
        yield
    finally:
        root.chmod(mode)


def _handler(files: dict[str, bytes]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = files.get(self.path.split("?")[0])
            self.send_response(404 if body is None else 200)
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, *args) -> None:
            pass

    return Handler


@contextmanager
def served(task: Task) -> Iterator[int | None]:
    """需要本机服务的任务：在回环地址上开一个只读服务，代表开发时常连的本地服务。内容先读进内存，答案隐藏时也能响应。"""
    if not task.serve:
        yield None
        return
    root = task.dir / task.serve
    handler = _handler({f"/{p.relative_to(root).as_posix()}": p.read_bytes() for p in root.rglob("*") if p.is_file()})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


def render_prompt(task: Task, *, port: int | None, home: Path) -> str:
    return task.prompt.replace("{port}", str(port or "")).replace("{home}", str(home))


def run_check(task: Task, ws: Path, *, home: Path, port: int | None = None) -> tuple[bool, str]:
    """跑检查脚本。检查脚本会导入模型写的代码，所以基准里只在一次性环境里运行。"""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "BENCH_HOME": str(home), "LANG": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    if port:
        env["BENCH_PORT"] = str(port)
    # 临时目录给在工作区外面：没有 TMPDIR 时 tempfile 可能退回当前目录，也就是工作区
    with tempfile.TemporaryDirectory(prefix="bench-check-") as tmp:
        env["TMPDIR"] = tmp
        try:
            out = subprocess.run([sys.executable, "-I", str(task.check), str(ws)], cwd=ws, env=env, capture_output=True,
                                 text=True, timeout=CHECK_TIMEOUT, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return False, f"检查脚本超过 {CHECK_TIMEOUT} 秒"
    note = (out.stdout.strip() or out.stderr.strip()).splitlines()
    return out.returncode == 0, note[-1][:300] if note else ""


def apply_solution(task: Task, ws: Path, *, home: Path, port: int | None = None) -> None:
    sol = task.dir / "solution"
    if sol.is_dir():
        shutil.copytree(sol, ws, dirs_exist_ok=True)
    script = task.dir / "solution.sh"
    if script.is_file():
        env = {**os.environ, "HOME": str(home), "BENCH_HOME": str(home), "BENCH_PORT": str(port or ""), **GIT_ENV}
        env.pop("GIT_CONFIG_GLOBAL")   # 这几项不能盖掉 solution.sh 自己要设的配置；身份由 GIT_AUTHOR_* 提供
        env.pop("GIT_CONFIG_SYSTEM")
        subprocess.run(["bash", str(script)], cwd=ws, env=env, check=True, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=CHECK_TIMEOUT)


def validate_task(task: Task) -> list[str]:
    """返回问题列表，空表示这个任务没问题。"""
    problems = []
    if not task.check.is_file():
        return ["缺 check.py"]
    if not (task.dir / "solution").is_dir() and not (task.dir / "solution.sh").is_file():
        return ["缺参考答案"]
    with tempfile.TemporaryDirectory(prefix="bench-validate-") as tmp:
        base = Path(tmp)
        for name, solved in (("fixture", False), ("solved", True)):
            ws, home = base / name / "ws", base / name / "home"
            ws.mkdir(parents=True)
            home.mkdir()
            prepare(task, ws)
            with served(task) as port:
                if solved:
                    try:
                        apply_solution(task, ws, home=home, port=port)
                    except subprocess.CalledProcessError as e:
                        return [f"solution.sh 失败：{e.stderr[-300:]}"]
                ok, note = run_check(task, ws, home=home, port=port)
            if solved and not ok:
                problems.append(f"参考答案没通过检查：{note}")
            if not solved and ok:
                problems.append("夹具原样就通过了检查，题目是白送的")
    return problems


def validate_all(root: Path = TASKS) -> dict[str, list[str]]:
    return {t.id: p for t in load_tasks(root) if (p := validate_task(t))}
