#!/usr/bin/env python3
"""干净安装的沙箱验收：不写任何配置，看文件、网络和子进程的边界是不是真的在内核这一层生效。

用已安装的 flywhl-harness 运行，不依赖仓库代码和 pytest，要用干净虚拟环境里的解释器::

    uv build --wheel
    uv venv "$HOME/fresh" && uv pip install --python "$HOME/fresh/bin/python" dist/*.whl
    "$HOME/fresh/bin/python" scripts/sandbox_acceptance.py --internet

每个场景在单独的进程里跑，HOME 指向当前目录下新建的目录。不放在 /tmp：Linux 沙箱里 /tmp 是私有的，凭证放在那里读不到说明不了什么，
所以同一个家目录里还放了一份普通文件做对照。载荷都是哨兵：读一个写着假内容的文件，创建一个空的标记文件。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SECRET = "not-a-real-secret-7f3a"
PLAIN = "plain-control-5c1e"
PREFIX = ".sandbox-acceptance-"
LEVEL = "macos" if sys.platform == "darwin" else "linux"

MCP_SERVER = r"""
import json, os, sys
def attempt(fn):
    try:
        return fn()
    except OSError as e:
        return type(e).__name__
for line in sys.stdin:
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:
        continue
    if method == "initialize":
        res = {"serverInfo": {"name": "probe"}}
    elif method == "tools/list":
        res = {"tools": [{"name": "peek", "inputSchema": {"type": "object", "properties": {}}}]}
    else:
        out = {"secret": attempt(lambda: open(sys.argv[1]).read()), "plain": attempt(lambda: open(sys.argv[2]).read()),
               "outside": attempt(lambda: open(sys.argv[3], "w").close() or "written"),
               "state": attempt(lambda: open(os.path.join(os.environ["FLYWHL_STATE_DIR"], "s"), "w").close() or "written")}
        res = {"content": [{"type": "text", "text": json.dumps(out)}]}
    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}), flush=True)
"""


class Paths:
    def __init__(self, base: Path):
        self.base, self.home, self.ws = base, base / "home", base / "ws"
        self.secret = self.home / ".ssh" / "probe_key"
        self.plain = self.home / "notes" / "plain.txt"
        self.outside = base / "outside.txt"

    def prepare(self) -> None:
        for p in (self.secret, self.plain, self.ws / "x"):
            p.parent.mkdir(parents=True, exist_ok=True)
        self.secret.write_text(SECRET)
        self.plain.write_text(PLAIN)
        # 经脚本读：命令文本里写明路径的话，命令分析先拒绝了，测不到内核这一层
        (self.ws / "read_secret.sh").write_text(f'cat "{self.secret}" 2>&1\n')
        (self.ws / "read_plain.sh").write_text(f'cat "{self.plain}" 2>&1\n')
        (self.ws / "mcp_probe.py").write_text(MCP_SERVER)

    def user_config(self, text: str) -> None:
        d = self.home / ".flywhl"
        d.mkdir(exist_ok=True)
        (d / "config.toml").write_text(text)


class Report:
    def __init__(self) -> None:
        self.ok = True

    def check(self, name: str, passed: bool, detail: object = "") -> None:
        self.ok = self.ok and bool(passed)
        print(json.dumps({"check": name, "ok": bool(passed), "detail": str(detail)[:300]}, ensure_ascii=False), flush=True)


class _Hello(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "5")
        self.end_headers()
        self.wfile.write(b"HELLO")

    def log_message(self, *a):
        pass


def serve() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Hello)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


async def drive(paths: Paths, steps: list[dict]) -> list[str]:
    """用脚本化模型跑一轮，返回每次工具调用的结果。配置只来自 HOME 和工作区，这里只把审批设成无人值守时放行。"""
    from flywhl_harness import E, Harness, HarnessConfig, ScriptedProvider

    cfg = HarnessConfig.load(workspace=str(paths.ws))
    cfg.permissions.on_ask = "allow"
    async with Harness(cfg, provider=ScriptedProvider([*steps, "好"]), model="scripted") as h:
        res = await h.run("验收")
        drive.box = h.confinement           # type: ignore[attr-defined]
    return [e.data["content"] for e in res.events if e.type == E.TOOL_END]


def bash(command: str) -> dict:
    return {"tool_calls": [{"name": "bash", "args": {"command": command}}]}


async def stage_default(paths: Paths, r: Report, args: argparse.Namespace) -> None:
    """什么配置都没有。"""
    port = serve()
    direct = (f"""{sys.executable} -I -S -c "import socket; socket.create_connection(('127.0.0.1', {port}), 3); print('CONNECTED')" 2>&1""")
    read, plain, written, outside, proxied, raw = await drive(paths, [
        bash("sh read_secret.sh"), bash("sh read_plain.sh"), bash("echo in > in.txt && cat in.txt"),
        bash(f'touch "{paths.outside}" 2>&1'), bash("curl -s -m 10 http://example.com/"), bash(direct)])
    box = drive.box                         # type: ignore[attr-defined]
    r.check("默认档位", (box.level, box.requested, box.network, box.rules) == (LEVEL, "auto", "allowlist", []), box.describe())
    r.check("对照：家目录里的普通文件读得到", PLAIN in plain, plain)
    r.check("凭证读不到", SECRET not in read, read)
    r.check("工作区能写", (paths.ws / "in.txt").exists(), written)
    r.check("工作区外写不了", not paths.outside.exists(), outside)
    r.check("没放行的主机被拦，报错写明主机和放行办法", "example.com" in proxied and "--net-allow" in proxied and "network_allow" in proxied, proxied)
    r.check("绕过代理直连也连不上", "CONNECTED" not in raw, raw)
    doctor = subprocess.run([str(Path(sys.executable).with_name("flywhl")), "doctor"], capture_output=True, text=True, cwd=paths.ws)
    r.check("flywhl doctor 显示实际生效的档位", f"系统沙箱 {LEVEL} · 网络 allowlist" in doctor.stdout, doctor.stdout[:200])


async def stage_allow(paths: Paths, r: Report, args: argparse.Namespace) -> None:
    """用户配置放行一个本机端口；``--internet`` 时再放行一个真实主机，走一遍 DNS 和 TLS。"""
    allowed, other = serve(), serve()
    rules = [f"localhost:{allowed}"] + (["example.com"] if args.internet else [])
    paths.user_config(f"[permissions]\nnetwork_allow = {json.dumps(rules)}\n")
    code = "curl -s -m 20 -o /dev/null -w '%{http_code}' "
    steps = [bash(f"curl -s -m 10 http://localhost:{allowed}/"), bash(code + f"http://localhost:{other}/")]
    if args.internet:
        steps += [bash(code + "https://example.com/"), bash(code + "https://example.org/")]
    out = await drive(paths, steps)
    r.check("放行的主机连得上", out[0].startswith("HELLO"), out[0])
    r.check("同一台机器上没放行的端口连不上", out[1].startswith("403"), out[1])
    if args.internet:
        r.check("放行的公网主机连得上（TLS 穿过代理）", out[2].startswith("200"), out[2])
        r.check("没放行的公网主机连不上", not out[3].startswith("200"), out[3])


async def stage_subprocess(paths: Paths, r: Report, args: argparse.Namespace) -> None:
    """命令 hook 和 MCP 服务器。"""
    ws = paths.ws
    hook = (f'sh {ws}/read_secret.sh > {ws}/hook-secret.txt; sh {ws}/read_plain.sh > {ws}/hook-plain.txt; '
            f'touch "{paths.outside}" 2>/dev/null; echo ok > "$FLYWHL_STATE_DIR/s" && echo written > {ws}/hook-state.txt; true')
    paths.user_config(f"""
[[hooks]]
event = "stop"
command = {json.dumps(hook)}

[mcp_servers.probe]
command = {json.dumps(sys.executable)}
args = {json.dumps(["-I", str(ws / "mcp_probe.py"), str(paths.secret), str(paths.plain), str(paths.outside)])}
""")
    out = json.loads((await drive(paths, [{"tool_calls": [{"name": "mcp__probe__peek", "args": {}}]}]))[0])
    r.check("MCP 服务器：对照读得到", out["plain"] == PLAIN, out)
    r.check("MCP 服务器：凭证读不到", SECRET not in out["secret"], out)
    r.check("MCP 服务器：工作区外写不了", out["outside"] != "written" and not paths.outside.exists(), out)
    r.check("MCP 服务器：有自己的可写目录", out["state"] == "written", out)
    r.check("命令 hook：对照读得到", PLAIN in (ws / "hook-plain.txt").read_text(), "")
    r.check("命令 hook：凭证读不到", SECRET not in (ws / "hook-secret.txt").read_text(), "")
    r.check("命令 hook：工作区外写不了", not paths.outside.exists(), "")
    r.check("命令 hook：有自己的可写目录", (ws / "hook-state.txt").exists(), "")


async def stage_unavailable(paths: Paths, r: Report, args: argparse.Namespace) -> None:
    """没有可用的沙箱：拒绝执行，给出办法；项目配置关不掉这条。"""
    marker = paths.ws / "ran.txt"
    paths.user_config("")
    (paths.ws / "flywhl.toml").write_text('[permissions]\nsandbox = "off"\n')
    out = (await drive(paths, [bash(f'/usr/bin/touch "{marker}"')]))[0]
    (paths.ws / "flywhl.toml").unlink()
    r.check("没有沙箱时拒绝执行，项目配置写 off 也不行", not marker.exists() and "命令未执行" in out, out)
    r.check("报错里有修法", 'sandbox = "off"' in out and "config.toml" in out, out)


async def stage_off(paths: Paths, r: Report, args: argparse.Namespace) -> None:
    """用户配置明确写了 off，才在没有沙箱的环境里运行。"""
    marker = paths.ws / "ran-off.txt"
    paths.user_config('[permissions]\nsandbox = "off"\n')
    await drive(paths, [bash(f'/usr/bin/touch "{marker}"')])
    r.check("用户配置写 off 后可以运行", marker.exists(), "")


STAGES = {"default": stage_default, "allow": stage_allow, "subprocess": stage_subprocess, "unavailable": stage_unavailable,
          "off": stage_off}


def no_sandbox(base: Path) -> tuple[list[str], dict[str, str]]:
    """让探测找不到沙箱的启动方式：Linux 上 PATH 里没有 bwrap；macOS 上套一层 sandbox-exec，内层起不来。"""
    if sys.platform == "darwin":
        return ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)"], {}
    return [], {"PATH": str(base)}


def run_stage(name: str, paths: Paths, internet: bool) -> tuple[bool, list[dict]]:
    env = {k: os.environ[k] for k in ("PATH", "TMPDIR", "LANG") if k in os.environ}
    env["HOME"] = str(paths.home)
    prefix: list[str] = []
    if name in ("unavailable", "off"):
        prefix, extra = no_sandbox(paths.base)
        env.update(extra)
    argv = [*prefix, sys.executable, os.path.abspath(__file__), "--stage", name, "--base", str(paths.base)]
    proc = subprocess.run([*argv, *(["--internet"] if internet else [])], capture_output=True, text=True, env=env, cwd=paths.ws,
                          timeout=240)
    rows = []
    for line in proc.stdout.splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    if proc.returncode or not rows:
        rows.append({"check": f"场景 {name} 跑完", "ok": False, "detail": (proc.stderr or proc.stdout)[-1500:]})
    return all(row["ok"] for row in rows), rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--internet", action="store_true", help="同时验证真实公网主机的放行与拦截")
    ap.add_argument("--json", help="把结果写成 JSON 文件")
    ap.add_argument("--stage", choices=STAGES, help=argparse.SUPPRESS)
    ap.add_argument("--base", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.stage:
        r = Report()
        asyncio.run(STAGES[args.stage](Paths(Path(args.base)), r, args))
        return 0 if r.ok else 1

    base = Path(tempfile.mkdtemp(prefix=PREFIX, dir=Path.cwd()))
    results, ok = [], True
    try:
        paths = Paths(base)
        paths.prepare()
        for name in STAGES:
            passed, rows = run_stage(name, paths, args.internet)
            ok = ok and passed
            for row in rows:
                print(f"{'通过' if row['ok'] else '失败'}  [{name}] {row['check']}" + ("" if row["ok"] else f"\n      {row['detail']}"))
                results.append({"stage": name, **row})
    finally:
        target = base.resolve()     # 删除前核对：只删这次 mkdtemp 建的目录
        if target.parent == Path.cwd().resolve() and target.name.startswith(PREFIX):
            shutil.rmtree(target, ignore_errors=True)
    if args.json:
        Path(args.json).write_text(json.dumps({"platform": sys.platform, "level": LEVEL, "ok": ok, "checks": results},
                                              ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n{'全部通过' if ok else '有失败项'}：{sum(x['ok'] for x in results)}/{len(results)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
