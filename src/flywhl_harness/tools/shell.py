"""bash 工具：在工作区内执行 shell 命令。

bash 是最强也最危险的工具。这里的工程要点：

- **进程组**：``start_new_session=True`` 让命令及其子进程同属一个进程组，取消/超时时可以
  一次性杀掉整棵进程树（``npm run dev`` 会派生多个子进程，只杀父进程会留下孤儿）。
- **两段式终止**：先 SIGTERM 给进程优雅退出的机会，宽限期后仍未退出再 SIGKILL。
- **流式输出**：边执行边通过 ``ctx.emit`` 推送输出，UI 能实时看到构建/测试日志。
- **输出截断保尾**：错误与结论通常在最后，所以保留结尾；完整输出外置为 blob 句柄。
- **密钥不可见**：默认从子进程环境中剥离名字含 KEY/TOKEN/SECRET/PASSWORD 的变量，
  模型即使执行 ``env`` 也看不到原始凭据。
- **动态副作用等级**：由命令分析器决定（``ls`` 是只读，``git push`` 是外部操作）。
- **可选的系统沙箱**：``sandbox="macos"`` 时命令跑在 sandbox-exec 里，凭证位置不可读、写操作限制在工作区与临时目录，
  见 ``tools/sandbox.py``。
"""

from __future__ import annotations

import asyncio
import codecs
import os
import re
import signal
import time
from typing import Any

from .._utils import truncate_tail
from ..policy.bash_analyzer import analyze
from .base import Effect, Tool, ToolContext, ToolResult
from .sandbox import SANDBOX_LEVELS, sandbox_argv

_SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.I)
OUT_LINES = 400
OUT_BYTES = 30 * 1024


def _clean_env(strip: bool) -> dict[str, str]:
    env = dict(os.environ)
    if strip:
        env = {k: v for k, v in env.items() if not _SECRET_ENV.search(k)}
    env.setdefault("PAGER", "cat")
    env["GIT_PAGER"] = "cat"
    env["NO_COLOR"] = "1"
    return env


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

    def __init__(self, strip_secret_env: bool = True, default_timeout: float = 120.0, sandbox: str = "off"):
        if sandbox not in SANDBOX_LEVELS:
            raise ValueError(f"未知的沙箱档位 {sandbox!r}（可选：{' | '.join(SANDBOX_LEVELS)}）")
        self.strip_secret_env = strip_secret_env
        self.default_timeout = default_timeout
        self.sandbox = sandbox

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
        if self.sandbox == "macos":
            try:
                argv = sandbox_argv(command, ctx.workspace.root)
            except RuntimeError as e:  # fail-closed：配置了沙箱却用不了，不能悄悄在沙箱外执行
                return ToolResult(content=f"沙箱不可用，命令未执行：{e}", is_error=True,
                                  details={"command": command, "sandbox": self.sandbox})
            # sandbox-exec 设置好沙箱后 exec 成 bash，进程号不变：取消、超时照样能整组终止
            proc = await asyncio.create_subprocess_exec(*argv, **spawn)
        else:
            proc = await asyncio.create_subprocess_shell(
                command, executable="/bin/bash" if os.path.exists("/bin/bash") else None, **spawn)
        chunks: list[bytes] = []
        timed_out = False
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

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
        if cut:
            ref = ctx.blobs.put(ctx.session_id, output)
            details["full_output_ref"] = ref
            body = f"[输出共 {total} 行，仅保留最后部分；完整输出句柄 {ref}，可用 read_ref 查看]\n" + body
        if timed_out:
            details["timed_out"] = True  # 执行器据此判断：外部命令超时 = 结果未知
            return ToolResult(content=f"{body}\n[命令超时（{timeout:.0f}s），已终止进程组]", is_error=True, details=details)
        status = f"\n[退出码 {proc.returncode}]"
        return ToolResult(content=(body or "(无输出)") + status, is_error=proc.returncode != 0, details=details)
