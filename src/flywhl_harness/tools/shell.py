"""bash 工具：在工作区内执行 shell 命令。

bash 是最强也最危险的工具。这里的工程要点：

- **进程组**：``start_new_session=True`` 让命令及其子进程同属一个进程组，取消/超时时可以
  一次性杀掉整棵进程树（``npm run dev`` 会派生多个子进程，只杀父进程会留下孤儿）。
- **两段式终止**：先 SIGTERM 给进程优雅退出的机会，宽限期后仍未退出再 SIGKILL。
- **流式输出**：边执行边通过 ``ctx.emit`` 推送输出，UI 能实时看到构建/测试日志。
- **输出截断保尾**：错误与结论通常在最后，所以保留结尾；完整输出外置为 blob 句柄。
- **密钥不可见**：默认从子进程环境中剥离看起来像凭据的变量——名字含 KEY/TOKEN/SECRET/PASSWORD 等字样，
  或以 ``_PWD``、``_PASS``、``_PAT``、``_DSN`` 结尾（``MYSQL_PWD``、``SMTP_PASS``、``GITHUB_PAT``），
  或值是带口令的 URL（``postgres://u:p@host/db``）。模型即使执行 ``env`` 也看不到原始凭据。
  这只管得了子进程自己的环境：``ps eww -p $PPID`` 读的是 harness 进程的环境，由命令分析按凭证位置拒绝。
- **动态副作用等级**：由命令分析器决定（``ls`` 是只读，``git push`` 是外部操作）。
- **系统沙箱**：默认 ``sandbox="auto"``，命令跑在 sandbox-exec / bubblewrap 里，凭证位置不可读、写操作限制在工作区与临时目录，
  本机 Unix 套接字（``unix_sockets`` 列出的除外）与钥匙串用不了，见 ``tools/sandbox.py``；网络默认只经出站代理连白名单里的主机，
  白名单默认是空的（``tools/egress.py``）。沙箱用不了时拒绝执行，不悄悄退回沙箱外，见 ``tools/confine.py``。
- **沙箱外执行**：``git push`` 走 SSH、``kubectl`` 这类要用凭证的命令在沙箱里会失败，``outside_sandbox: true`` 申请这一条在沙箱外
  执行。它是一扇每次都要人开的门：任何模式下都问人，规则、会话授权、``auto_approve`` 都覆盖不到，无人值守时直接拒绝，见 ``human_gate``。
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import os
import re
import signal
import time
from typing import Any

from .._utils import truncate_tail
from ..policy.bash_analyzer import analyze
from .base import Effect, Tool, ToolContext, ToolResult
from .confine import Confinement, SandboxUnavailable

_SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|AUTH|COOKIE"
                         r"|_(PWD|PASS|PAT|DSN)(_|$)", re.I)   # _PWD 要带前缀：PWD / OLDPWD 是当前目录
_URL_WITH_PASSWORD = re.compile(r"^[a-z][a-z0-9+.-]*://[^/\s:@]*:[^/\s@]+@", re.I)
OUT_LINES = 400
OUT_BYTES = 30 * 1024


# 只读的 git 命令也会执行仓库配置里的命令：git status 会调用 core.fsmonitor 指定的程序、写索引时触发 post-index-change
# hook；仓库里“伪装”成裸仓库的目录（自带 HEAD / config / objects）被 git -C 进入时，它的 config 同样生效。
# 用命令行级配置（优先级最高）关掉 fsmonitor、只认显式指定的裸仓库，并让只读命令不顺手写索引
_GIT_HARDENING = (("core.fsmonitor", "false"), ("safe.bareRepository", "explicit"))
# 名字撞上密钥规则的普通变量：GIT_CONFIG_KEY_<n> 是配置键名，GIT_AUTHOR_* 是提交身份（撞的是 AUTH）
_NOT_SECRET_ENV = re.compile(r"GIT_CONFIG_KEY_\d+|GIT_AUTHOR_(NAME|EMAIL|DATE)")
_GIT_CONFIG_KEY = re.compile(r"GIT_CONFIG_KEY_(\d+)")


def _renumber_git_config(env: dict[str, str], n: int) -> int:
    """值被当成密钥剥掉之后，键和 GIT_CONFIG_COUNT 还在，git 会报错退出：去掉这一对，其余的重新编号，返回剩下的对数。"""
    pairs = []
    for i in sorted(int(m[1]) for m in map(_GIT_CONFIG_KEY.fullmatch, list(env)) if m and int(m[1]) < n):
        key, value = env.pop(f"GIT_CONFIG_KEY_{i}"), env.pop(f"GIT_CONFIG_VALUE_{i}", None)
        if value is not None:
            pairs.append((key, value))
    for i, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = key, value
    return len(pairs)


def _clean_env(strip: bool) -> dict[str, str]:
    env = dict(os.environ)
    if strip:   # 删掉 GIT_CONFIG_KEY_<n> 而留着 GIT_CONFIG_COUNT，git 会直接报错；删掉 GIT_AUTHOR_* 提交时会报身份未知
        env = {k: v for k, v in env.items()
               if not ((_SECRET_ENV.search(k) and not _NOT_SECRET_ENV.fullmatch(k)) or _URL_WITH_PASSWORD.match(v))}
    env.setdefault("PAGER", "cat")
    env["GIT_PAGER"] = "cat"
    env["NO_COLOR"] = "1"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        n = max(0, int(env.get("GIT_CONFIG_COUNT", "0")))   # 接在用户已有的 GIT_CONFIG_* 后面
    except ValueError:
        n = 0
    if strip:
        n = _renumber_git_config(env, n)
    for i, (key, value) in enumerate(_GIT_HARDENING, start=n):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = key, value
    env["GIT_CONFIG_COUNT"] = str(n + len(_GIT_HARDENING))
    return env


# 不走 HTTP 代理的客户端在受限网络里的典型报错：走代理的会拿到 403，由 egress_denied 说明
_NETWORK_FAILURE = re.compile(r"could not resolve|name resolution|name or service not known|nodename nor servname"
                              r"|network is unreachable|no address associated|getaddrinfo", re.I)
_SANDBOX_REFUSED = re.compile(r"operation not permitted|read-only file system|permission denied", re.I)


async def _reap_group(pgid: int, grace: float = 1.0) -> None:
    """命令退出后，进程组里还在运行的后台进程：先 SIGTERM，宽限期后 SIGKILL。自己另起会话的进程不在组里，管不到。"""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


async def _terminate(proc: asyncio.subprocess.Process, grace: float = 2.0) -> None:
    """先 SIGTERM，宽限期后 SIGKILL；作用于整个进程组。"""
    if proc.returncode is not None:     # shell 已经退出，还占着 stdout 的是它留下的后台进程
        await _reap_group(proc.pid)
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        await asyncio.wait_for(proc.wait(), grace)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError, PermissionError):   # 进程组刚好走完：macOS 报 PermissionError
            os.killpg(pgid, signal.SIGKILL)
        await proc.wait()


def _sandbox_hint(box: Confinement, output: str, denied: bool) -> str:
    """命令失败、输出又像是被沙箱拦下时，告诉模型该怎么办，免得它换着写法反复重试。"""
    if not denied and box.network != "allow" and _NETWORK_FAILURE.search(output):
        return (f"\n[命令在系统沙箱里运行，网络策略是 {box.network}：沙箱里没有 DNS，不走 HTTP 代理的客户端连不出去。"
                "需要的主机加进 permissions.network_allow 或 --net-allow；SSH 这类不走代理的连接用 outside_sandbox 申请]")
    if not denied and _SANDBOX_REFUSED.search(output):
        return "\n[命令在系统沙箱里运行，读不到凭证，只能写工作区与临时目录。确实需要时用 outside_sandbox 申请在沙箱外执行，要人工确认]"
    return ""


class BashTool(Tool):
    name = "bash"
    reads_workspace = True
    description = (
        "在工作区根目录执行 bash 命令，返回合并后的 stdout/stderr 与退出码。适合运行测试、构建、git 等。"
        "长输出只保留结尾部分（完整输出会给出句柄，可用 read_ref 查看）。"
        "不要用它执行交互式命令或常驻服务；需要等待时请设置 timeout（秒）。"
        "命令默认在系统沙箱里运行：读不到凭证，只能写工作区与临时目录，网络只能连白名单里的主机。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
            "timeout": {"type": "integer", "minimum": 1, "maximum": 1800, "description": "超时秒数，默认 120"},
            "outside_sandbox": {"type": "boolean", "description": "这条命令必须用到沙箱外的凭证或网络时才设为 true"
                                                                  "（git push、kubectl、gh）。每次都要人工确认，无人值守时直接拒绝"},
        },
        "required": ["command"],
        "additionalProperties": False,
    }
    effect = Effect.WRITE
    interruptible = True
    manages_timeout = True

    def __init__(self, strip_secret_env: bool = True, default_timeout: float = 120.0, sandbox: str = "auto",
                 sandbox_network: str = "auto", network_allow: list[str] | None = None,
                 unix_sockets: list[str] | None = None, protected: list[str] | None = None):
        self.strip_secret_env = strip_secret_env
        self.default_timeout = default_timeout
        self.confinement = Confinement.resolve(sandbox, sandbox_network, network_allow, unix_sockets, protected)

    def human_gate(self, args: dict[str, Any]) -> str | None:
        if args.get("outside_sandbox") and self.confinement.active:
            return "这条命令将在沙箱外执行：能读凭证、能写工作区之外的文件，网络不受限"
        return None

    def describe_call(self, args: dict[str, Any]) -> str:
        text = super().describe_call(args)
        return f"{text}（将在沙箱外执行）" if self.human_gate(args) else text

    def effect_for(self, args: dict[str, Any], workspace: str | None = None) -> Effect:
        level = analyze(str(args.get("command", "")), workspace).level
        return {"read": Effect.READ, "write": Effect.WRITE}.get(level, Effect.EXTERNAL)

    def untrusted_for(self, args: dict[str, Any]) -> bool:
        # curl / wget / git pull 取回的是外部内容：可能夹带提示注入，之后的副作用操作要升级为人工确认
        return analyze(str(args.get("command", ""))).network

    def interruptible_for(self, args: dict[str, Any], workspace: str | None = None) -> bool:
        # 已经发出的外部操作（git push、curl POST、部署……）半途打断会留下“不知道成没成功”的状态：
        # 给它宽限期跑完，超时才标记为结果未知；本地命令可以直接杀进程组
        return self.effect_for(args, workspace) != Effect.EXTERNAL

    @property
    def concurrency_safe(self) -> bool:
        return False  # 即使是只读命令也串行执行：命令间常有隐式顺序依赖（先 build 再 test）

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = args["command"]
        timeout = float(args.get("timeout") or self.default_timeout)
        if ctx.time_left is not None:  # 截止时间沿调用链传递：不超过 Run 剩余的墙钟预算
            timeout = max(1.0, min(timeout, ctx.time_left))
        started = time.monotonic()
        spawn = dict(cwd=str(ctx.workspace.root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                     stdin=asyncio.subprocess.DEVNULL, start_new_session=True)
        outside = self.human_gate(args) is not None
        if outside and not ctx.human_approved:   # 只看参数不够：回放和直接调用走不到审批，这里默认拒绝
            return ToolResult(content="这条命令要在沙箱外执行，必须由人当场确认，这次调用没有经过确认：命令未执行", is_error=True,
                              details={"command": command, "outside_sandbox": True})
        box = Confinement() if outside else self.confinement
        shell = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
        chunks: list[bytes] = []
        timed_out = False
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        proxy = None
        try:
            async with box.enter([shell, "-c", command], ctx.workspace.root, _clean_env(self.strip_secret_env)) as run:
                proxy = run.proxy
                # sandbox-exec 设置好沙箱后 exec 成 bash，进程号不变；bwrap 是 bash 的父进程，--die-with-parent 保证它退出时
                # 沙箱里的进程一起退出。两种情况取消、超时都能整组终止
                proc = await asyncio.create_subprocess_exec(*run.argv, env=run.env, **spawn)

                async def pump() -> None:
                    assert proc.stdout is not None
                    while True:
                        data = await proc.stdout.read(4096)
                        if not data:
                            tail = decoder.decode(b"", final=True)
                            if tail:
                                ctx.emit({"output": tail})
                            break
                        chunks.append(data)
                        text = decoder.decode(data)  # 增量解码：被分块切开的多字节字符留到下一块再输出
                        if text:
                            ctx.emit({"output": text})

                try:
                    await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout)
                except asyncio.TimeoutError:
                    timed_out = True
                    await _terminate(proc)
                except asyncio.CancelledError:
                    await _terminate(proc)  # 被取消：杀掉整棵进程树后再把取消向上传播
                    raise
                finally:
                    if proxy is not None:   # 代理随命令关闭，沙箱放行的那个端口号可能被别的服务复用：留在后台的进程一并结束
                        await _reap_group(proc.pid)
        except SandboxUnavailable as e:  # fail-closed：配置了沙箱却用不了，不能悄悄在沙箱外执行
            return ToolResult(content=f"沙箱不可用，命令未执行：{e}", is_error=True,
                              details={"command": command, "sandbox": box.level, "sandbox_network": box.network})

        output = b"".join(chunks).decode("utf-8", errors="replace")
        body, cut, total = truncate_tail(output, OUT_LINES, OUT_BYTES)
        details: dict[str, Any] = {
            "command": command,
            "exit_code": proc.returncode,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "total_lines": total,
        }
        if box.level != "off":
            details["sandbox"] = box.level
            details["sandbox_network"] = box.network
        if outside:
            details["outside_sandbox"] = True
        if proxy and proxy.denied:
            details["egress_denied"] = sorted(set(proxy.denied))
            first = details["egress_denied"][0].rsplit(":", 1)[0]
            body += (f"\n[出站白名单拦下了 {'、'.join(details['egress_denied'])}。确实需要的话把主机加进 permissions.network_allow，"
                     f"或者启动时加 --net-allow {first}]")
        if cut:
            ref = ctx.blobs.put(ctx.session_id, output)
            details["full_output_ref"] = ref
            body = f"[输出共 {total} 行，仅保留最后部分；完整输出句柄 {ref}，可用 read_ref 查看]\n" + body
        if timed_out:
            details["timed_out"] = True  # 执行器据此判断：外部命令超时 = 结果未知
            return ToolResult(content=f"{body}\n[命令超时（{timeout:.0f}s），已终止进程组]", is_error=True, details=details)
        if box.level == "linux" and proc.returncode == 1 and output.startswith("bwrap: "):
            body = f"[沙箱没能启动，命令未执行。容器里运行时，Docker 默认遮住了 /proc 的部分路径，见文档“Linux：bubblewrap 档位”]\n{body}"
        elif proc.returncode != 0 and box.level != "off":
            body += _sandbox_hint(box, output[-4000:], bool(proxy and proxy.denied))
        status = f"\n[退出码 {proc.returncode}]"
        return ToolResult(content=(body or "(无输出)") + status, is_error=proc.returncode != 0, details=details)
