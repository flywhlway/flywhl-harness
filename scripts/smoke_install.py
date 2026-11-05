#!/usr/bin/env python3
"""干净安装的冒烟：只靠 pip 装好的包，能不能在空目录里把 Web 控制台和演示模型跑起来。

发布流水线用它检查 wheel，手动也可以跑。要用干净虚拟环境里的解释器，只依赖标准库和已安装的包::

    uv build --wheel
    uv venv "$HOME/fresh" && uv pip install --python "$HOME/fresh/bin/python" dist/*.whl
    "$HOME/fresh/bin/python" scripts/smoke_install.py

查的是用户装完马上会碰到的几件事：命令行入口、版本号、静态资源进了 wheel、没有密钥时用内置演示模型、一次只读任务能跑完。
HOME 和工作区都是新建的临时目录，环境变量里的密钥全部去掉；演示任务只列目录，不执行命令。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from importlib.metadata import version
from pathlib import Path
from typing import Any

TOKEN = secrets.token_hex(16)   # 不用 token_urlsafe：可能以 - 开头，命令行会当成选项
FLYWHL = Path(sys.executable).parent / "flywhl"
DONE = {"completed", "failed", "cancelled", "budget_exhausted", "loop_detected"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def request(port: int, path: str, body: dict[str, Any] | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def api(port: int, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    status, raw = request(port, path, body)
    if status != 200:
        raise SystemExit(f"{path} 返回 {status}：{raw[:300]!r}")
    return json.loads(raw)


def wait_ready(port: int, proc: subprocess.Popen[str], log: Path) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"flywhl serve 提前退出（{proc.returncode}）：\n{log.read_text(errors='replace')}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise SystemExit(f"30 秒内没有开始监听：\n{log.read_text(errors='replace')}")


def check(name: str, ok: bool, detail: Any = "") -> None:
    print(f"{'通过' if ok else '失败'}  {name}{f'：{detail}' if detail != '' and not ok else ''}", flush=True)
    if not ok:
        raise SystemExit(1)


def main() -> None:
    expected = version("flywhl-harness")
    with tempfile.TemporaryDirectory(prefix="flywhl-smoke-") as tmp:
        root = Path(tmp).resolve()
        home, ws, log = root / "home", root / "empty-workspace", root / "serve.log"
        home.mkdir()
        ws.mkdir()
        env = {k: v for k, v in os.environ.items() if not re.search(r"KEY|TOKEN|SECRET|^LLM_", k)}
        env.update(HOME=str(home), FLYWHL_HOME=str(home / ".flywhl"))

        out = subprocess.run([FLYWHL, "--version"], env=env, capture_output=True, text=True, timeout=30)
        check("命令行入口与版本号", out.returncode == 0 and out.stdout.strip() == expected, out.stdout + out.stderr)

        port = free_port()
        with log.open("w") as f:
            proc = subprocess.Popen([FLYWHL, "serve", "-w", str(ws), "--port", str(port), "--token", TOKEN],
                                    env=env, cwd=root, stdout=f, stderr=subprocess.STDOUT, text=True)
        try:
            wait_ready(port, proc, log)
            status, page = request(port, "/")
            check("静态资源进了 wheel", status == 200 and b"<html" in page.lower(), (status, page[:120]))

            info = api(port, "/api/info")
            check("没有密钥时用演示模型", info.get("version") == expected and info.get("model") == "flywhl-demo",
                  {k: info.get(k) for k in ("version", "provider", "model")})

            session = api(port, "/api/sessions", {"title": "smoke"})
            run = api(port, f"/api/sessions/{session['id']}/runs", {"prompt": "列出目录"})
            deadline = time.monotonic() + 30
            while run.get("status") not in DONE and time.monotonic() < deadline:
                time.sleep(0.2)
                run = api(port, f"/api/runs/{run['id']}")
            check("演示任务跑完", run.get("status") == "completed", run)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
    print(f"全部通过：flywhl-harness {expected}")


if __name__ == "__main__":
    main()
