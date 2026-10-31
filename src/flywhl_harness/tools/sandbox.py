"""系统沙箱档位（原型）：bash 子进程跑在 macOS Seatbelt 或 Linux bubblewrap 里，由内核兜住命令分析漏掉的部分。

命令分析只能看字符串：``D=$HOME/.ku; cat ${D}be/config`` 这种变量拼接、glob、编码过的路径它都看不到，
bypass 模式下又没有人审，漏掉就是照读。这一档让内核按真实路径判定：

- **凭证位置不可读写**：``policy/sensitive.py`` 的 ``CREDENTIAL_GLOBS`` 转成 Seatbelt 正则，与文件工具、命令分析共用一份清单；
  ``$KUBECONFIG`` 等环境变量指向别处的，按 harness 进程里的取值一并拒绝（配置每条命令生成一次）；
- **写操作限制在工作区与临时目录**，工作区里 harness 自己的 ``.flywhl/`` 与 ``flywhl.toml`` 除外（与托管基线一致）。

刻意留小的范围：

- 只包 bash 工具的子进程；命令 hook、MCP 服务器子进程不在沙箱里；
- 从 ``(allow default)`` 起步，主要收紧文件访问；进程、Mach 服务（含钥匙串查询）不限制。网络默认不限：
  ``sandbox_network = "deny"`` 禁止一切对外连接，包括回环地址上的 TCP 与本机 Unix 套接字（DNS 查询、docker.sock 都走它）；
  ``"allowlist"`` 在此基础上只放行到本机出站代理的连接，代理按 ``network_allow`` 判定主机，见 ``tools/egress.py``；
- 凭证位置读不到，``ssh``、``git push``（SSH 协议）、``kubectl``、``gh`` 这类要用凭证的命令在沙箱里会失败——这一档用能力换安全；
- ``sandbox-exec`` 已被 Apple 标为 deprecated，但仍随系统提供。不可用时 **拒绝执行**，不悄悄退回沙箱外。

Linux 档（``sandbox = "linux"``）用 bubblewrap，规则对齐 macOS，做法不同：

- bubblewrap 只能按路径挂载，不能按模式拒绝。凭证清单落到家目录与工作区下的具体位置：目录换成空的 tmpfs，文件换成
  ``/dev/null``；散落在别处的私钥文件（``*id_rsa*``）认不出。``/proc`` 是新 PID 命名空间里的，看不到别的进程的环境变量；
- 根目录只读挂载，工作区与临时目录可写，``.flywhl/`` 与 ``flywhl.toml`` 再只读挂回去（工作区里还不存在的
  ``flywhl.toml`` 拦不住新建，新建的项目配置仍要经过信任确认）；
- ``deny`` 另起网络命名空间（只剩回环），并把 ``/run`` 换成空目录：docker.sock、D-Bus、systemd-resolved 的套接字都在那里，
  网络命名空间隔不开文件系统上的 Unix 套接字；``allowlist`` 还没有实现，配置了就拒绝执行。
"""

from __future__ import annotations

import glob
import os
import pwd
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Iterable

from ..policy.sensitive import CREDENTIAL_GLOBS

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
SANDBOX_LEVELS = ("off", "macos", "linux")
SANDBOX_NETWORK = ("allow", "deny", "allowlist")
# 指向凭证位置的环境变量：值是文件的按文件拒绝，是目录的按整个目录拒绝；KUBECONFIG 可以是冒号分隔的多个文件
_CREDENTIAL_ENV_FILES = ("KUBECONFIG", "AWS_SHARED_CREDENTIALS_FILE")
_CREDENTIAL_ENV_DIRS = ("GNUPGHOME", "DOCKER_CONFIG", "CLOUDSDK_CONFIG", "GH_CONFIG_DIR")
# 命令正常运行需要写的设备文件；块设备不在其中
_DEVICES = ('(literal "/dev/null")', '(literal "/dev/zero")', '(literal "/dev/dtracehelper")',
            '(regex #"^/dev/fd/")', '(regex #"^/dev/tty")')


def _str(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _glob_regex(glob: str) -> str:
    """fnmatch 的 ``*`` 可以跨目录，这里同样转成 ``.*``；清单里只用到 ``*`` 和普通字符。
    字母写成 ``[aA]``：清单按小写写、匹配不区分大小写（理由见 ``policy/sensitive.py``），磁盘上却是 ``Library/Keychains``。"""
    def one(c: str) -> str:
        if c == "*":
            return ".*"
        if c.isalpha():
            return f"[{c.lower()}{c.upper()}]"
        return "\\" + c if c in ".^$+?()[]{}|\\" else c
    return f'(regex #"^{"".join(one(c) for c in glob)}$")'


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
                     env: dict[str, str] | None = None, network: str = "allow",
                     local_ports: Iterable[str] = ()) -> str:
    """生成 Seatbelt 配置。``writable`` 为工作区之外允许写的目录，默认是临时目录；``env`` 默认取当前环境；
    ``network`` 取 ``SANDBOX_NETWORK`` 之一，``allowlist`` 时 ``local_ports`` 是放行的本机端口（出站代理的端口在其中）。"""
    if network not in SANDBOX_NETWORK:
        raise ValueError(f"未知的沙箱网络策略 {network!r}（可选：{' | '.join(SANDBOX_NETWORK)}）")
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
    if network != "allow":
        # 数据外带、下载执行都要走网络。Unix 套接字一并拒绝：DNS 查询经 mDNSResponder 的套接字，数据能编码进域名带出去
        lines += ["(deny network-outbound (remote ip))", "(deny network-outbound (remote unix-socket))"]
        # 放行要写在两条拒绝之后：Unix 套接字那条写在放行后面时，连到本机端口也会被拒（实测）
        lines += [f'(allow network-outbound (remote ip "localhost:{p}"))' for p in (local_ports if network == "allowlist" else ())]
    return "\n".join(lines)


def sandbox_argv(command: str, workspace: str | Path, *, writable: Iterable[str] | None = None,
                 executable: str | None = None, network: str = "allow", local_ports: Iterable[str] = ()) -> list[str]:
    """把 bash 命令包进 sandbox-exec。沙箱不可用时抛 ``RuntimeError``，由调用方拒绝执行。"""
    executable = executable or SANDBOX_EXEC
    if sys.platform != "darwin" or not os.path.exists(executable):
        raise RuntimeError(f"sandbox = \"macos\" 需要 macOS 自带的 {executable}，当前环境没有")
    profile = seatbelt_profile(workspace, writable=writable, network=network, local_ports=local_ports)
    return [executable, "-p", profile, "/bin/bash", "-c", command]


def _credential_paths(workspace: str, env: dict[str, str]) -> list[str]:
    """凭证清单在家目录与工作区下的具体位置（只取存在的）。真实家目录与 $HOME 不同时（容器、测试）两个都查。"""
    bases = {os.path.realpath(os.path.expanduser("~")), os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir), workspace}
    found = set()
    for g in CREDENTIAL_GLOBS:
        if g.startswith("*/"):
            rel = g[2:].removesuffix("/*")
            found |= {p for base in bases for p in glob.glob(os.path.join(glob.escape(base), rel))}
        elif "*" not in g and os.path.exists(g):
            found.add(g)
    for name in _CREDENTIAL_ENV_FILES:
        found |= {p for p in env.get(name, "").split(os.pathsep) if p and os.path.exists(p)}
    found |= {env[n] for n in _CREDENTIAL_ENV_DIRS if env.get(n) and os.path.exists(env[n])}
    return sorted({os.path.realpath(p) for p in found})


def bwrap_argv(command: str, workspace: str | Path, *, writable: Iterable[str] | None = None,
               env: dict[str, str] | None = None, network: str = "allow", executable: str | None = None) -> list[str]:
    """把 bash 命令包进 bubblewrap。不可用、或要求了还没实现的出站白名单时抛 ``RuntimeError``，由调用方拒绝执行。"""
    bwrap = executable or shutil.which("bwrap")
    if not sys.platform.startswith("linux") or not bwrap or not os.path.exists(bwrap):
        raise RuntimeError('sandbox = "linux" 需要 Linux 上的 bubblewrap（bwrap），当前环境没有')
    if network not in SANDBOX_NETWORK:
        raise ValueError(f"未知的沙箱网络策略 {network!r}（可选：{' | '.join(SANDBOX_NETWORK)}）")
    if network == "allowlist":
        raise RuntimeError('Linux 沙箱还不支持出站白名单（sandbox_network = "allowlist"），可以改用 "deny"')
    ws = os.path.realpath(workspace)
    argv = [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--unshare-pid", "--unshare-ipc", "--die-with-parent", "--new-session"]
    if network == "deny":   # 网络命名空间隔不开文件系统上的 Unix 套接字：/run 里的 docker.sock、D-Bus 一并遮住
        argv += ["--unshare-net", "--tmpfs", "/run"]
    # 挂载有先后：后挂的盖住先挂的。可写的上层目录先挂，工作区其次，再把 harness 状态与凭证位置盖回去
    extra = [p for p in ("/tmp", "/var/tmp", tempfile.gettempdir())] if writable is None else list(writable)
    for p in sorted({os.path.realpath(p) for p in extra if os.path.isdir(p)}, key=len):
        argv += ["--bind", p, p]
    argv += ["--bind", ws, ws]
    for p in (os.path.join(ws, ".flywhl"), os.path.join(ws, "flywhl.toml")):
        if os.path.exists(p):
            argv += ["--ro-bind", p, p]
    for p in _credential_paths(ws, dict(os.environ) if env is None else env):
        argv += ["--tmpfs", p] if os.path.isdir(p) else ["--ro-bind", "/dev/null", p]
    return [*argv, "--chdir", ws, "/bin/bash", "-c", command]


__all__ = ["SANDBOX_EXEC", "SANDBOX_LEVELS", "SANDBOX_NETWORK", "bwrap_argv", "default_writable", "sandbox_argv",
           "seatbelt_profile"]
