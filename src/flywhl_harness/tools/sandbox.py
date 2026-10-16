"""macOS sandbox-exec 档位（原型）：bash 子进程跑在 Seatbelt 配置下，由内核兜住命令分析漏掉的部分。

命令分析只能看字符串：``D=$HOME/.ku; cat ${D}be/config`` 这种变量拼接、glob、编码过的路径它都看不到，
bypass 模式下又没有人审，漏掉就是照读。这一档让内核按真实路径判定：

- **凭证位置不可读写**：``policy/sensitive.py`` 的 ``CREDENTIAL_GLOBS`` 转成 Seatbelt 正则，与文件工具、命令分析共用一份清单；
  ``$KUBECONFIG`` 等环境变量指向别处的，按 harness 进程里的取值一并拒绝（配置每条命令生成一次）；
- **写操作限制在工作区与临时目录**，工作区里 harness 自己的 ``.flywhl/`` 与 ``flywhl.toml`` 除外（与托管基线一致）。

刻意留小的范围：

- 只包 bash 工具的子进程；命令 hook、MCP 服务器子进程不在沙箱里；
- 从 ``(allow default)`` 起步，只收紧文件访问：网络、进程、Mach 服务（含钥匙串查询）都不限制，出口管控要另做；
- 凭证位置读不到，``ssh``、``git push``（SSH 协议）、``kubectl``、``gh`` 这类要用凭证的命令在沙箱里会失败——这一档用能力换安全；
- ``sandbox-exec`` 已被 Apple 标为 deprecated，但仍随系统提供。不可用时 **拒绝执行**，不悄悄退回沙箱外。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Iterable

from ..policy.sensitive import CREDENTIAL_GLOBS

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
SANDBOX_LEVELS = ("off", "macos")
# 指向凭证位置的环境变量：值是文件的按文件拒绝，是目录的按整个目录拒绝；KUBECONFIG 可以是冒号分隔的多个文件
_CREDENTIAL_ENV_FILES = ("KUBECONFIG", "AWS_SHARED_CREDENTIALS_FILE")
_CREDENTIAL_ENV_DIRS = ("GNUPGHOME", "DOCKER_CONFIG", "CLOUDSDK_CONFIG", "GH_CONFIG_DIR")
# 命令正常运行需要写的设备文件；块设备不在其中
_DEVICES = ('(literal "/dev/null")', '(literal "/dev/zero")', '(literal "/dev/dtracehelper")',
            '(regex #"^/dev/fd/")', '(regex #"^/dev/tty")')


def _str(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _glob_regex(glob: str) -> str:
    """fnmatch 的 ``*`` 可以跨目录，这里同样转成 ``.*``；清单里只用到 ``*`` 和普通字符。"""
    body = "".join(".*" if c == "*" else ("\\" + c if c in ".^$+?()[]{}|\\" else c) for c in glob)
    return f'(regex #"^{body}$")'


def _credential_filters(env: dict[str, str]) -> list[str]:
    out = [_glob_regex(g) for g in CREDENTIAL_GLOBS]
    for name in _CREDENTIAL_ENV_FILES:
        out += [f"(literal {_str(os.path.realpath(p))})" for p in env.get(name, "").split(os.pathsep) if p]
    for name in _CREDENTIAL_ENV_DIRS:
        if env.get(name):
            out.append(f"(subpath {_str(os.path.realpath(env[name]))})")
    return out


def default_writable() -> list[str]:
    """工作区之外允许写的位置：临时目录（heredoc、编译器、pytest 都要用）。"""
    return sorted({os.path.realpath(tempfile.gettempdir()), "/private/tmp", "/private/var/folders"})


def seatbelt_profile(workspace: str | Path, *, writable: Iterable[str] | None = None,
                     env: dict[str, str] | None = None) -> str:
    """生成 Seatbelt 配置。``writable`` 为工作区之外允许写的目录，默认是临时目录；``env`` 默认取当前环境。"""
    ws = os.path.realpath(workspace)
    extra = default_writable() if writable is None else [os.path.realpath(p) for p in writable]
    allowed = " ".join([f"(subpath {_str(ws)})", *(f"(subpath {_str(p)})" for p in extra), *_DEVICES])
    lines = [
        "(version 1)",
        "(allow default)",
        f"(deny file-read* file-write* {' '.join(_credential_filters(dict(os.environ) if env is None else env))})",
        f"(deny file-write* (require-not (require-any {allowed})))",
        f"(deny file-write* (subpath {_str(os.path.join(ws, '.flywhl'))}) (literal {_str(os.path.join(ws, 'flywhl.toml'))}))",
    ]
    return "\n".join(lines)


def sandbox_argv(command: str, workspace: str | Path, *, writable: Iterable[str] | None = None,
                 executable: str | None = None) -> list[str]:
    """把 bash 命令包进 sandbox-exec。沙箱不可用时抛 ``RuntimeError``，由调用方拒绝执行。"""
    executable = executable or SANDBOX_EXEC
    if sys.platform != "darwin" or not os.path.exists(executable):
        raise RuntimeError(f"sandbox = \"macos\" 需要 macOS 自带的 {executable}，当前环境没有")
    return [executable, "-p", seatbelt_profile(workspace, writable=writable), "/bin/bash", "-c", command]


__all__ = ["SANDBOX_EXEC", "SANDBOX_LEVELS", "default_writable", "sandbox_argv", "seatbelt_profile"]
