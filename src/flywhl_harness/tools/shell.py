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
- **可选的系统沙箱**：``sandbox="macos"`` / ``"linux"`` 时命令跑在 sandbox-exec / bubblewrap 里，凭证位置不可读、
  写操作限制在工作区与临时目录，本机 Unix 套接字（``unix_sockets`` 列出的除外）与钥匙串用不了，见 ``tools/sandbox.py``；
  网络可以全断，macOS 上还可以只经出站代理连白名单里的主机（``tools/egress.py``）。
  网络策略离不开沙箱：配置了 ``deny`` / ``allowlist`` 却没有开沙箱时拒绝执行，不悄悄连外网。
"""

from __future__ import annotations

import asyncio
import atexit
import codecs
import contextlib
import os
import re
import shutil
import signal
import tempfile
import time
from typing import Any

from .._utils import truncate_tail
from ..policy.bash_analyzer import analyze
from .base import Effect, Tool, ToolContext, ToolResult
from .egress import EgressProxy, local_ports, parse_rules
from .sandbox import SANDBOX_LEVELS, SANDBOX_NETWORK, bwrap_argv, sandbox_argv

_SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|AUTH|COOKIE"
                         r"|_(PWD|PASS|PAT|DSN)(_|$)", re.I)   # _PWD 要带前缀：PWD / OLDPWD 是当前目录
_URL_WITH_PASSWORD = re.compile(r"^[a-z][a-z0-9+.-]*://[^/\s:@]*:[^/\s@]+@", re.I)
OUT_LINES = 400
OUT_BYTES = 30 * 1024


# 只读的 git 命令也会执行仓库配置里的命令：git status 会调用 core.fsmonitor 指定的程序、写索引时触发 post-index-change
# hook；仓库里“伪装”成裸仓库的目录（自带 HEAD / config / objects）被 git -C 进入时，它的 config 同样生效。
# 用命令行级配置（优先级最高）关掉 fsmonitor、只认显式指定的裸仓库，并让只读命令不顺手写索引
_GIT_HARDENING = (("core.fsmonitor", "false"), ("safe.bareRepository", "explicit"))
_GIT_CONFIG_KEY = re.compile(r"GIT_CONFIG_KEY_\d+")


def _clean_env(strip: bool) -> dict[str, str]:
    env = dict(os.environ)
    if strip:   # GIT_CONFIG_KEY_<n> 是配置键名（名字里带 KEY），删掉它而留着 GIT_CONFIG_COUNT，git 会直接报错
        env = {k: v for k, v in env.items()
               if not ((_SECRET_ENV.search(k) and not _GIT_CONFIG_KEY.fullmatch(k)) or _URL_WITH_PASSWORD.match(v))}
    env.setdefault("PAGER", "cat")
    env["GIT_PAGER"] = "cat"
    env["NO_COLOR"] = "1"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        n = max(0, int(env.get("GIT_CONFIG_COUNT", "0")))   # 接在用户已有的 GIT_CONFIG_* 后面
    except ValueError:
        n = 0
    for i, (key, value) in enumerate(_GIT_HARDENING, start=n):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = key, value
    env["GIT_CONFIG_COUNT"] = str(n + len(_GIT_HARDENING))
    return env


_PRIVATE_TMP_PREFIX = "flywhl-sandbox-tmp-"


def _remove_private_tmp(path: str) -> None:
    """删除前核对目标：只删 mkdtemp 在系统临时目录下建出来的那一个。"""
    target = os.path.realpath(path)
    if (os.path.dirname(target) == os.path.realpath(tempfile.gettempdir())
            and os.path.basename(target).startswith(_PRIVATE_TMP_PREFIX)):
        shutil.rmtree(target, ignore_errors=True)


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
    if proc.returncode is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        await asyncio.wait_for(proc.wait(), grace)
    except asyncio.TimeoutError:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()


class BashTool(Tool):
    name = "bash"
    description = (
        "在工作区根目录执行 bash 命令，返回合并后的 stdout/stderr 与退出码。适合运行测试、构建、git 等。"
        "长输出只保留结尾部分（完整输出会给出句柄，可用 read_ref 查看）。"
        "不要用它执行交互式命令或常驻服务；需要等待时请设置 timeout（秒）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
            "timeout": {"type": "integer", "minimum": 1, "maximum": 1800, "description": "超时秒数，默认 120"},
        },
        "required": ["command"],
        "additionalProperties": False,
    }
    effect = Effect.WRITE
    interruptible = True
    manages_timeout = True

    def __init__(self, strip_secret_env: bool = True, default_timeout: float = 120.0, sandbox: str = "off",
                 sandbox_network: str = "allow", network_allow: list[str] | None = None,
                 unix_sockets: list[str] | None = None):
        if sandbox not in SANDBOX_LEVELS:
            raise ValueError(f"未知的沙箱档位 {sandbox!r}（可选：{' | '.join(SANDBOX_LEVELS)}）")
        if sandbox_network not in SANDBOX_NETWORK:
            raise ValueError(f"未知的沙箱网络策略 {sandbox_network!r}（可选：{' | '.join(SANDBOX_NETWORK)}）")
        if not isinstance(network_allow or [], list):
            raise ValueError(f"network_allow 必须是字符串列表，实际是 {network_allow!r}")
        sockets = unix_sockets or []
        if not isinstance(sockets, list) or not all(isinstance(p, str) and os.path.isabs(p) for p in sockets):
            raise ValueError(f"unix_sockets 必须是绝对路径的列表，实际是 {unix_sockets!r}")
        self.strip_secret_env = strip_secret_env
        self.default_timeout = default_timeout
        self.sandbox = sandbox
        self.sandbox_network = sandbox_network
        self.network_allow = parse_rules(network_allow or [])   # 写法不对在启动时报错
        self.unix_sockets = sockets
        self._private_tmp: str | None = None

    def private_tmp(self) -> str:
        """Linux 档本会话私有的 /tmp：命令之间照样能用临时文件传东西，又看不到宿主 /tmp 里的套接字。进程退出时删除。"""
        if self._private_tmp is None:
            self._private_tmp = tempfile.mkdtemp(prefix=_PRIVATE_TMP_PREFIX)
            atexit.register(_remove_private_tmp, self._private_tmp)
        return self._private_tmp

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
                     stdin=asyncio.subprocess.DEVNULL, start_new_session=True, env=_clean_env(self.strip_secret_env))
        if self.sandbox == "off" and self.sandbox_network != "allow":   # fail-closed：网络限制要靠沙箱执行
            return ToolResult(content=f"命令未执行：sandbox_network = {self.sandbox_network!r} 要在系统沙箱里才能生效，"
                                      "当前 sandbox = \"off\"。请同时设置 permissions.sandbox", is_error=True,
                              details={"command": command, "sandbox": self.sandbox, "sandbox_network": self.sandbox_network})
        chunks: list[bytes] = []
        timed_out = False
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        proxy: EgressProxy | None = None
        async with contextlib.AsyncExitStack() as stack:   # 出站代理与命令同生共死
            if self.sandbox != "off":
                if self.sandbox == "macos" and self.sandbox_network == "allowlist":
                    proxy = await stack.enter_async_context(EgressProxy(self.network_allow))
                    spawn["env"] = {**spawn["env"], **proxy.env()}
                try:
                    argv = (sandbox_argv(command, ctx.workspace.root, network=self.sandbox_network,
                                         local_ports=[str(proxy.port), *local_ports(self.network_allow)] if proxy else (),
                                         unix_sockets=self.unix_sockets)
                            if self.sandbox == "macos" else
                            bwrap_argv(command, ctx.workspace.root, network=self.sandbox_network,
                                       private_tmp=self.private_tmp(), unix_sockets=self.unix_sockets))
                except RuntimeError as e:  # fail-closed：配置了沙箱却用不了，不能悄悄在沙箱外执行
                    return ToolResult(content=f"沙箱不可用，命令未执行：{e}", is_error=True,
                                      details={"command": command, "sandbox": self.sandbox})
                # sandbox-exec 设置好沙箱后 exec 成 bash，进程号不变；bwrap 是 bash 的父进程，--die-with-parent 保证它退出时
                # 沙箱里的进程一起退出。两种情况取消、超时都能整组终止
                proc = await asyncio.create_subprocess_exec(*argv, **spawn)
            else:
                proc = await asyncio.create_subprocess_shell(
                    command, executable="/bin/bash" if os.path.exists("/bin/bash") else None, **spawn)

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

        output = b"".join(chunks).decode("utf-8", errors="replace")
        body, cut, total = truncate_tail(output, OUT_LINES, OUT_BYTES)
        details: dict[str, Any] = {
            "command": command,
            "exit_code": proc.returncode,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "total_lines": total,
        }
        if self.sandbox != "off":
            details["sandbox"] = self.sandbox
            details["sandbox_network"] = self.sandbox_network
        if proxy and proxy.denied:
            details["egress_denied"] = sorted(set(proxy.denied))
            body += f"\n[出站白名单拦下了 {'、'.join(details['egress_denied'])}；确实需要的话加进 permissions.network_allow]"
        if cut:
            ref = ctx.blobs.put(ctx.session_id, output)
            details["full_output_ref"] = ref
            body = f"[输出共 {total} 行，仅保留最后部分；完整输出句柄 {ref}，可用 read_ref 查看]\n" + body
        if timed_out:
            details["timed_out"] = True  # 执行器据此判断：外部命令超时 = 结果未知
            return ToolResult(content=f"{body}\n[命令超时（{timeout:.0f}s），已终止进程组]", is_error=True, details=details)
        if self.sandbox == "linux" and proc.returncode == 1 and output.startswith("bwrap: "):
            body = f"[沙箱没能启动，命令未执行。容器里运行时，Docker 默认遮住了 /proc 的部分路径，见文档“Linux：bubblewrap 档位”]\n{body}"
        status = f"\n[退出码 {proc.returncode}]"
        return ToolResult(content=(body or "(无输出)") + status, is_error=proc.returncode != 0, details=details)
