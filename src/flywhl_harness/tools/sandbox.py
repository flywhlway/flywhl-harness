"""系统沙箱：bash、命令 hook、MCP 服务器、验收命令跑在 macOS Seatbelt 或 Linux bubblewrap 里，由内核兜住命令分析漏掉的部分。

默认 ``sandbox = "auto"``：``detect()`` 真跑一次探测能用的档位，用不了时拒绝执行、不悄悄退回沙箱外，只有用户级配置或命令行
明确写了 ``off`` 才放行。本模块只生成沙箱的启动参数；出站代理、独立目录这些运行时的事在 ``tools/confine.py``。

命令分析只能看命令文本：``D=$(echo $HOME/.ku); cat ${D}be/config`` 这种运行时才拼出来的路径、glob、编码过的路径它都看不到，
bypass 模式下又没有人审，漏掉就是照读。这一档让内核按真实路径判定：

- **凭证位置不可读写**：``policy/sensitive.py`` 的 ``CREDENTIAL_GLOBS`` 转成 Seatbelt 正则，与文件工具、命令分析共用一份清单；
  ``$KUBECONFIG`` 等环境变量指向别处的，按 harness 进程里的取值一并拒绝（配置每条命令生成一次）；
- **写操作限制在工作区与临时目录**，工作区里 harness 自己的 ``.flywhl/`` 与 ``flywhl.toml`` 除外（与托管基线一致）；
- **本机进程间通信不能绕开前两条**：docker.sock 能起一个挂载家目录的容器，tmux 能替它在沙箱外敲命令，ssh-agent 能用私钥，
  ``open`` / ``osascript`` 让沙箱外的进程替它执行，钥匙串与剪贴板里就是凭证。Unix 套接字默认只放行 DNS 与系统日志，
  其余要在 ``unix_sockets`` 里逐个列出（不能在沙箱可写的目录里）；钥匙串、剪贴板、LaunchServices / Apple 事件的 Mach 服务
  与 launchd 建任务一律拒绝。

刻意留小的范围：

- 从 ``(allow default)`` 起步，收紧文件访问与上面这些 IPC 通道，其余进程间通信不限制。网络策略：
  ``sandbox_network = "deny"`` 禁止一切对外连接，包括回环地址上的 TCP 与 DNS 查询（走 mDNSResponder 的套接字）；
  ``"allowlist"`` 在此基础上只放行到本机出站代理的连接，代理按 ``network_allow`` 判定主机，见 ``tools/egress.py``；
  ``allow`` 不限 TCP / UDP，回环地址上的本机服务照样连得上；
- 凭证位置读不到，``ssh``、``git push``（SSH 协议）、``kubectl``、``gh`` 这类要用凭证的命令在沙箱里会失败——这一档用能力换安全，
  要在沙箱外执行的命令走 ``outside_sandbox``，每次都要人确认；
- ``sandbox-exec`` 已被 Apple 标为 deprecated，但仍随系统提供。不可用时 **拒绝执行**，不悄悄退回沙箱外。

Linux 档（``sandbox = "linux"``）用 bubblewrap，规则对齐 macOS，做法不同：

- bubblewrap 只能按路径挂载，不能按模式拒绝。凭证清单落到具体位置：家目录下按清单查，工作区逐层遍历（跳过依赖与缓存目录，
  条目太多数不完时拒绝执行）；目录换成空的 tmpfs，文件换成 ``/dev/null``。散落在工作区与家目录之外的私钥文件认不出。
  ``/proc`` 是新 PID 命名空间里的，看不到别的进程的环境变量；
- 根目录只读挂载，工作区可写，``.flywhl/`` 与 ``flywhl.toml`` 再只读挂回去（工作区里还不存在的
  ``flywhl.toml`` 拦不住新建，新建的项目配置仍要经过信任确认）；
- Unix 套接字没有按连接拒绝的规则，只读挂载也挡不住连接，只能让它们不出现在沙箱里：``/run``（docker.sock、D-Bus、
  ``XDG_RUNTIME_DIR``）与 ``/var/tmp`` 换成空目录，``/tmp``（X11、tmux、ssh-agent）换成本会话私有的目录，其余位置上的
  （家目录里的 Docker Desktop、colima……）按 ``/proc/net/unix`` 逐个盖成 ``/dev/null``；``unix_sockets`` 列出的再挂回来。
  ``allow`` 与宿主共用网络命名空间，抽象命名空间里的套接字（不在文件系统上，如部分 X11、D-Bus）仍然连得上；
- ``deny`` 另起网络命名空间（只剩回环，抽象套接字也隔开了）。``allowlist`` 同样另起网络命名空间，出站代理的 Unix 套接字
  挂进沙箱，沙箱里起一个回环端口转发器（``_RELAY``），``HTTP(S)_PROXY`` 指向它：主机判定全在沙箱外的代理里，
  转发器被命令杀掉或改掉，损失的只是它自己的网络。``localhost:5432`` 这类本机端口规则，转发器在沙箱回环上监听同一个端口，
  经代理连到宿主，不支持 HTTP 代理的客户端照样能用；``localhost:*`` 列不出端口，只能走代理。
"""

from __future__ import annotations

import contextlib
import fnmatch
import functools
import glob
import logging
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..policy.sensitive import CREDENTIAL_GLOBS

log = logging.getLogger(__name__)

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
SANDBOX_LEVELS = ("auto", "off", "macos", "linux")
SANDBOX_NETWORK = ("auto", "allow", "deny", "allowlist")   # auto：有沙箱时 allowlist，明确关掉沙箱时 allow
_NETWORK_POLICIES = SANDBOX_NETWORK[1:]
EGRESS_SOCKET = "/run/flywhl-egress.sock"   # 出站代理的 Unix 套接字在 Linux 沙箱里的位置
EGRESS_PORT = 17890                         # 沙箱回环上的转发端口。网络命名空间是这条命令独占的，端口不会冲突
# 指向凭证位置的环境变量：值是文件的按文件拒绝，是目录的按整个目录拒绝；KUBECONFIG 可以是冒号分隔的多个文件
_CREDENTIAL_ENV_FILES = ("KUBECONFIG", "AWS_SHARED_CREDENTIALS_FILE")
_CREDENTIAL_ENV_DIRS = ("GNUPGHOME", "DOCKER_CONFIG", "CLOUDSDK_CONFIG", "GH_CONFIG_DIR")
# 命令正常运行需要写的设备文件；块设备不在其中
_DEVICES = ('(literal "/dev/null")', '(literal "/dev/zero")', '(literal "/dev/dtracehelper")',
            '(regex #"^/dev/fd/")', '(regex #"^/dev/tty")')
# 拒绝的 Mach 服务：文件钥匙串（security、git credential-osxkeychain）、数据保护钥匙串、剪贴板，
# 以及 LaunchServices / Apple 事件。coreservices 只能整组拒绝：单独拒绝其中任何一个，open 都还有别的路把程序拉起来（实测）
_MACH_DENY = ('(global-name "com.apple.SecurityServer")', '(global-name "com.apple.securityd.xpc")',
              '(global-name "com.apple.securityd.general")', '(global-name "com.apple.pasteboard.1")',
              '(global-name-regex #"^com\\.apple\\.coreservices\\.")')
# allow 档仍放行的 Unix 套接字：DNS 查询与系统日志
_UNIX_SYSTEM = ("/private/var/run/mDNSResponder", "/private/var/run/syslog")
# Linux 档遍历工作区找凭证：跳过依赖与缓存目录（文件多，也不是用户放凭证的地方），条目上限防止大仓库每条命令都慢
_CREDENTIAL_RE = re.compile("|".join(fnmatch.translate(g) for g in CREDENTIAL_GLOBS))
_WALK_SKIP = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache", ".pytest_cache",
                        ".ruff_cache", ".gradle", ".next", "target"})
_WALK_LIMIT = 50_000
_SCAN_LIMIT = 4096   # 找改过名的套接字时，一个目录里最多看这么多条目


# 沙箱里的回环端口转发器：argv = 套接字 代理端口 本机端口(逗号分隔) 要执行的命令...
# 先绑好端口、再 fork，命令一启动就连得上；父进程 exec 成命令本身（退出码、标准输入输出都是它的），子进程转发。
# 命令退出时 PID 命名空间整个销毁，转发器随之结束。它不做任何判定，见模块注释
_RELAY = r"""
import os, socket, sys, threading

def pump(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
        dst.shutdown(socket.SHUT_WR)
    except OSError:
        pass

def relay(conn, path, port):
    up = socket.socket(socket.AF_UNIX)
    try:
        up.connect(path)
        if port:
            up.sendall(b"CONNECT localhost:%d HTTP/1.1\r\n\r\n" % port)
            head = b""
            while b"\r\n\r\n" not in head:
                data = up.recv(4096)
                if not data:
                    raise OSError("proxy closed")
                head += data
            status, _, rest = head.partition(b"\r\n\r\n")
            if b" 200 " not in status.split(b"\r\n", 1)[0]:
                raise OSError("proxy refused")
            if rest:   # 服务器先开口的协议（MySQL、SSH）：问候可能和代理的回复粘在一起
                conn.sendall(rest)
    except OSError:
        conn.close()
        up.close()
        return
    threading.Thread(target=pump, args=(up, conn), daemon=True).start()
    pump(conn, up)

def serve(listener, path, port):
    while True:
        conn, _ = listener.accept()
        threading.Thread(target=relay, args=(conn, path, port), daemon=True).start()

path, proxy_port, local = sys.argv[1], int(sys.argv[2]), [int(p) for p in sys.argv[3].split(",") if p]
listeners = []
for port in [proxy_port, *local]:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        if port == proxy_port:
            raise
        s.close()    # 1024 以下的端口绑不上：这条规则仍然能经 HTTP 代理走，不能让一个端口拖垮所有命令
        continue
    s.listen(128)
    listeners.append((s, 0 if port == proxy_port else port))
if os.fork() == 0:
    null = os.open(os.devnull, os.O_RDWR)   # 不占着标准输出：harness 要靠 EOF 判断命令的输出结束
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    for s, port in listeners:
        threading.Thread(target=serve, args=(s, path, port), daemon=True).start()
    threading.Event().wait()
os.execvp(sys.argv[4], sys.argv[4:])
"""


@dataclass(frozen=True)
class Detected:
    level: str          # macos | linux；用不了时为空
    reason: str = ""    # 用不了的原因与修法


def _trial(argv: list[str]) -> str:
    """真跑一次，成功返回空串，失败返回错误输出。"""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    return "" if r.returncode == 0 else (r.stderr.strip() or f"退出码 {r.returncode}")[:300]


def probe() -> Detected:
    """探测这台机器能用的档位。文件在不代表能用：sandbox-exec 不能嵌套，容器和内核策略会拦下 user namespace，都要真跑一次，
    Linux 上要带齐实际运行时用的命名空间。"""
    if sys.platform == "darwin":
        if not os.path.exists(SANDBOX_EXEC):
            return Detected("", f"没有 {SANDBOX_EXEC}")
        # 配置要真有限制：外层已有沙箱时内层会被拒绝，只放行一切的配置却照样成功
        err = _trial([SANDBOX_EXEC, "-p", '(version 1)(allow default)(deny file-write* (literal "/nonexistent/flywhl-probe"))',
                      "/usr/bin/true"])
        return Detected("", f"sandbox-exec 启动失败（已经在别的沙箱里运行时不能嵌套）：{err}") if err else Detected("macos")
    if not sys.platform.startswith("linux"):
        return Detected("", f"{sys.platform} 上没有可用的系统沙箱")
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return Detected("", "没有安装 bubblewrap，用包管理器装上：apt install bubblewrap 或 dnf install bubblewrap")
    err = _trial([bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-pid", "--unshare-ipc",
                  "--unshare-net", "--die-with-parent", "--new-session", "/bin/sh", "-c", ":"])
    if not err:
        return Detected("linux")
    return Detected("", f"bubblewrap 启动失败：{err}。多半是 user namespace 被禁用：Ubuntu 上运行 sudo sysctl "
                        "kernel.apparmor_restrict_unprivileged_userns=0；在容器里要放开 seccomp 和 /proc 遮蔽"
                        "（--security-opt seccomp=unconfined --security-opt systempaths=unconfined）")


@functools.lru_cache(maxsize=1)
def detect() -> Detected:
    """``probe()`` 的进程级缓存：一个进程只探测一次，环境不会在运行中变化。"""
    return probe()


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
                     local_ports: Iterable[str] = (), unix_sockets: Iterable[str] = (),
                     protected: Iterable[str] = ()) -> str:
    """生成 Seatbelt 配置。``writable`` 为工作区之外允许写的目录，默认是临时目录；``env`` 默认取当前环境；
    ``network`` 取 ``SANDBOX_NETWORK`` 之一，``allowlist`` 时 ``local_ports`` 是放行的本机端口（出站代理的端口在其中）；
    ``unix_sockets`` 是另外放行连接的 Unix 套接字；``protected`` 见 ``_read_only()``。"""
    if network not in _NETWORK_POLICIES:
        raise ValueError(f"未知的沙箱网络策略 {network!r}（可选：{' | '.join(_NETWORK_POLICIES)}）")
    ws = os.path.realpath(workspace)
    extra = default_writable() if writable is None else [os.path.realpath(p) for p in writable]
    allowed = " ".join([f"(subpath {_str(ws)})", *(f"(subpath {_str(p)})" for p in extra), *_DEVICES])
    _refuse_writable(unix_sockets, [ws, *extra])
    read_only = _read_only(ws, protected)
    frozen = [f"(subpath {_str(p)})" for p in read_only]
    if os.path.isdir(os.path.join(ws, ".git")):   # 整个 .git 换掉就绕过了里面的只读。只管目录本身，里面照常写
        frozen.append(f"(literal {_str(os.path.join(ws, '.git'))})")
    # 规则写的是路径，上级目录改个名，里面的东西就不再匹配。工作区在临时目录这类可写位置下时改得了，所以路径上每一级都不许动
    frozen += [f"(literal {_str(p)})" for p in sorted({ws, *(str(q) for f in (ws, *read_only) for q in Path(f).parents)})]
    lines = [
        "(version 1)",
        "(allow default)",
        f"(deny file-read* file-write* {' '.join(_credential_filters(dict(os.environ) if env is None else env))})",
        f"(deny file-write* (require-not (require-any {allowed})))",
        f"(deny file-write* (subpath {_str(os.path.join(ws, '.flywhl'))}) (literal {_str(os.path.join(ws, 'flywhl.toml'))})"
        f" {' '.join(frozen)})",
        f"(deny mach-lookup {' '.join(_MACH_DENY)})",
        "(deny job-creation)",   # launchd 任务同样跑在沙箱外。实测 launchd 已经不接受沙箱进程提交的任务，这里写明而不依赖它
        # Unix 套接字连着本机的守护进程（docker.sock、tmux、ssh-agent），任何网络策略下都先整体拒绝
        "(deny network-outbound (remote unix-socket))",
    ]
    if network != "allow":   # 数据外带、下载执行都要走网络
        lines.append("(deny network-outbound (remote ip))")
    # 放行要写在拒绝之后：Unix 套接字那条写在放行后面时，连到本机端口也会被拒（实测）。
    # allow 档放行 DNS 与系统日志；deny / allowlist 档连 DNS 也不放：查询经 mDNSResponder 的套接字，数据能编码进域名带出去
    lines += [f'(allow network-outbound (remote ip "localhost:{p}"))' for p in (local_ports if network == "allowlist" else ())]
    # 内核按解析后的真实路径判定（实测）：放行位置上被换成指向别处的符号链接时，连接照样被拒
    for p in dict.fromkeys([*(_UNIX_SYSTEM if network == "allow" else ()), *unix_sockets]):
        lines.append(f"(allow network-outbound (remote unix-socket (path-literal {_str(p)})))")
    return "\n".join(lines)


_GIT_REDIRECTS = ("commondir", "config.worktree")   # git 用它们决定去哪读配置和钩子，写得了它们就等于写得了 config


def git_redirects_to_sweep(workspace: str | Path) -> list[str]:
    """Linux 上要盯着的文件名。不存在的文件挂不成只读，bwrap 留下的空占位文件又会让 git 读 ``commondir`` 时报错退出，
    所以由 ``sweep_git_redirects`` 在运行期间反复清理。``commondir`` 只该出现在 ``.git/worktrees/<名字>/`` 里，主仓库的
    ``.git`` 里有就是放进来的，不管什么时候放的。``config.worktree`` 可能是用户自己的，只盯着现在还没有的情况。"""
    mine = os.path.lexists(os.path.join(os.path.realpath(workspace), ".git", "config.worktree"))
    return ["commondir", *([] if mine else ["config.worktree"])]


def sweep_git_redirects(workspace: str | Path, names: Iterable[str]) -> None:
    """删掉 ``.git`` 里的这些文件。``.git`` 可能被沙箱里的命令换成了软链接，所以不跟随，删的只能是工作区自己的。"""
    try:
        fd = os.open(os.path.join(os.path.realpath(workspace), ".git"), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return
    try:
        for name in names:
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=fd)
                log.warning("工作区 .git 里出现了 %s，git 会照着它读配置和钩子，已删除", name)
    finally:
        os.close(fd)


def _git_frozen(ws: str) -> list[str]:
    """``.git`` 里写不得的位置。工作区还没有 ``.git`` 时什么都不保护：``git init`` 要能在空工作区里运行。

    - ``.git`` 是目录：``hooks`` 没有也要保护，``git init --template=`` 建出来的仓库就没有它，沙箱里建一个就是沙箱外执行的钩子；
      ``commondir``、``config.worktree`` 把 git 引到别处读配置，``worktrees`` 里是其他 linked worktree 的同类文件；
    - ``.git`` 是文件（linked worktree、子模块）：``gitdir:`` 指向哪里，用的就是哪份配置，所以文件本身只读。

    路径和解析后的路径都算：``hooks`` 是软链接时，把链接换掉也绕过了只读。"""
    git = os.path.join(ws, ".git")
    if not os.path.isdir(git):
        return [git] if os.path.lexists(git) else []
    hooks, config = os.path.join(git, "hooks"), os.path.join(git, "config")
    names = [hooks, *([config] if os.path.exists(config) else []), *(os.path.join(git, n) for n in (*_GIT_REDIRECTS, "worktrees"))]
    return list(dict.fromkeys([*names, *map(os.path.realpath, names)]))


def _read_only(ws: str, protected: Iterable[str]) -> list[str]:
    """工作区里写不得的位置，都是之后会在沙箱外起作用的东西：``protected`` 是 harness 的数据目录（影子仓库、会话库），
    ``.git`` 里的内容在用户下一次 ``git commit`` 或一条获准离开沙箱的 ``git push`` 时执行，见 ``_git_frozen()``。"""
    paths = [*_git_frozen(ws), *(os.path.realpath(p) for p in protected)]
    return [p for p in paths if not _under(ws, p)]   # 工作区放在数据目录里时，不能把整个工作区变成只读


def frozen_paths(workspace: str | Path, protected: Iterable[str] = ()) -> list[str]:
    """沙箱里写不得的全部位置。bash 靠沙箱本身挡住；文件工具跑在沙箱外，靠人工门查这份清单，两边要一致。"""
    ws = os.path.realpath(workspace)
    return [os.path.join(ws, ".flywhl"), os.path.join(ws, "flywhl.toml"), *_read_only(ws, protected)]


def frozen_landing(path: str | Path, frozen: Iterable[str]) -> str | None:
    """``path`` 落进 ``frozen`` 的哪一处。按文件身份比，软链接、硬链接、不分大小写的卷上的变体都绕不过；还不存在的按路径比。"""
    real = os.path.realpath(path)
    chain = [real, *map(str, Path(real).parents)]
    for place in frozen:
        for p in chain:
            if p.casefold() == place.casefold():
                return place
            try:
                if os.path.samefile(p, place):
                    return place
            except OSError:
                pass
    return None


def _program(command: str | list[str]) -> list[str]:
    return ["/bin/bash", "-c", command] if isinstance(command, str) else list(command)


def sandbox_argv(command: str | list[str], workspace: str | Path, *, writable: Iterable[str] | None = None,
                 executable: str | None = None, network: str = "allow", local_ports: Iterable[str] = (),
                 unix_sockets: Iterable[str] = (), protected: Iterable[str] = ()) -> list[str]:
    """把命令包进 sandbox-exec：字符串按 bash 命令，列表按完整的 argv。沙箱不可用时抛 ``RuntimeError``，由调用方拒绝执行。"""
    executable = executable or SANDBOX_EXEC
    if sys.platform != "darwin" or not os.path.exists(executable):
        raise RuntimeError(f"sandbox = \"macos\" 需要 macOS 自带的 {executable}，当前环境没有")
    profile = seatbelt_profile(workspace, writable=writable, network=network, local_ports=local_ports,
                               unix_sockets=unix_sockets, protected=protected)
    return [executable, "-p", profile, *_program(command)]


def pin_sockets(paths: Iterable[str]) -> list[tuple[str, str]]:
    """``unix_sockets`` 启动时定下真实位置：(配置的路径, 目录部分解析过符号链接、最后一级保留原样的路径)。
    之后每条命令都用这个结果、不再解析——沙箱能写的目录（macOS 的 /tmp）里，套接字可能被换成指向 docker.sock 的符号链接，
    每次重新解析就会把 docker.sock 放进来。最后一级不解析：放行位置上是符号链接时，连接按它指向的真实路径判定，不会放行。"""
    return [(p, os.path.join(os.path.realpath(os.path.dirname(p)), os.path.basename(p))) for p in paths]


def _under(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def _refuse_writable(sockets: Iterable[str], writable: Iterable[str]) -> None:
    """放行的套接字不能落在沙箱可写的目录里：命令能把那个位置换成别的套接字的硬链接（同一个 inode，路径照样对得上），
    或者把上层目录整个换掉。符号链接挡得住（连接按真实路径判定），这两种挡不住。目录部分按真实路径再比一次。"""
    dirs = list(writable)
    for s in sockets:
        real = os.path.join(os.path.realpath(os.path.dirname(s)), os.path.basename(s))
        if any(_under(p, w) for p in (s, real) for w in dirs):
            raise RuntimeError(f"unix_sockets 里的 {s} 在沙箱可写的目录里，命令能把它换成别的套接字；"
                               "换个位置再放行（例如把服务的套接字目录设到临时目录之外），或者改用本机 TCP 端口")


def host_sockets() -> list[str]:
    """宿主上绑定在文件系统上的 Unix 套接字（``/proc/net/unix``，本网络命名空间里的全部）：比遍历目录完整，也便宜。
    抽象命名空间里的（``@`` 开头）与按相对路径绑定的不在其中。"""
    try:
        with open("/proc/net/unix", encoding="utf-8", errors="replace") as f:
            rows = f.read().splitlines()[1:]
    except OSError:
        return []
    return sorted({cols[7] for cols in (r.split(None, 7) for r in rows) if len(cols) == 8 and cols[7].startswith("/")})


def workspace_credentials(workspace: str) -> set[str]:
    """工作区里任何深度的凭证位置：目录本身是凭证位置的（``.ssh``、``.aws``）整个算上、不再往下找。
    条目超过上限时抛 ``RuntimeError``：数不完就没法确认遮住了全部凭证，宁可不执行。"""
    found: set[str] = set()
    seen = 0
    for top, dirs, files in os.walk(workspace):
        seen += len(dirs) + len(files)
        if seen > _WALK_LIMIT:
            raise RuntimeError(f"工作区里的文件超过 {_WALK_LIMIT} 个，没法逐个确认凭证位置都已遮住，可以在更小的目录里启动")
        keep = []
        for d in dirs:
            p = os.path.join(top, d)
            if _CREDENTIAL_RE.match(p.lower() + "/x"):
                found.add(p)
            elif d not in _WALK_SKIP:
                keep.append(d)   # 指向目录的符号链接 os.walk 不会进去；它指向的凭证按真实路径另行遮住
        dirs[:] = keep
        found |= {p for f in files if _CREDENTIAL_RE.match((p := os.path.join(top, f)).lower())}
    return found


def _credential_paths(workspace: str, env: dict[str, str]) -> list[str]:
    """凭证清单在家目录下的具体位置（只取存在的）与工作区里任何深度的凭证位置。真实家目录与 $HOME 不同时（容器、测试）两个都查。"""
    homes = {os.path.realpath(os.path.expanduser("~")), os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)}
    found = workspace_credentials(workspace)
    for g in CREDENTIAL_GLOBS:
        if g.startswith("*/"):
            rel = g[2:].removesuffix("/*")
            found |= {p for base in homes for p in glob.glob(os.path.join(glob.escape(base), rel))}
        elif "*" not in g and os.path.exists(g):
            found.add(g)
    for name in _CREDENTIAL_ENV_FILES:
        found |= {p for p in env.get(name, "").split(os.pathsep) if p and os.path.exists(p)}
    found |= {env[n] for n in _CREDENTIAL_ENV_DIRS if env.get(n) and os.path.exists(env[n])}
    return sorted({os.path.realpath(p) for p in found})


def _relay_python() -> str:
    """转发器用的解释器。沙箱里 /tmp、/run 是空的，装在那里的虚拟环境看不到；只用标准库，解析到真实位置或系统的 python3 都行。"""
    for candidate in (sys.executable, shutil.which("python3") or ""):
        real = os.path.realpath(candidate) if candidate else ""
        if real and os.path.exists(real) and not any(_under(real, d) for d in ("/tmp", "/var/tmp", "/run", "/var/run")):
            return real
    return ""


def require_bwrap(executable: str | None = None) -> str:
    bwrap = executable or shutil.which("bwrap")
    if not sys.platform.startswith("linux") or not bwrap or not os.path.exists(bwrap):
        raise RuntimeError('sandbox = "linux" 需要 Linux 上的 bubblewrap（bwrap），当前环境没有')
    return bwrap


def bwrap_argv(command: str | list[str], workspace: str | Path, *, writable: Iterable[str] | None = None,
               env: dict[str, str] | None = None, network: str = "allow", executable: str | None = None,
               private_tmp: str | None = None, unix_sockets: Iterable[tuple[str, str]] = (),
               egress_socket: str | None = None, local_ports: Iterable[str] = (), chdir: str | None = None,
               protected: Iterable[str] = ()) -> list[str]:
    """把命令包进 bubblewrap：字符串按 bash 命令，列表按完整的 argv。不可用、工作区大到数不完凭证位置时抛 ``RuntimeError``，
    由调用方拒绝执行。``private_tmp`` 是挂成沙箱里 ``/tmp`` 的宿主目录（会话内的命令共用），不给就每条命令一个空的；
    ``unix_sockets`` 是 ``pin_sockets()`` 定下的 (配置的路径, 真实位置)；``network = "allowlist"`` 时 ``egress_socket`` 是
    宿主上出站代理的 Unix 套接字，``local_ports`` 是白名单里的本机端口；``chdir`` 默认是工作区；``protected`` 见 ``_read_only()``。"""
    bwrap = require_bwrap(executable)
    if network not in _NETWORK_POLICIES:
        raise ValueError(f"未知的沙箱网络策略 {network!r}（可选：{' | '.join(_NETWORK_POLICIES)}）")
    python = _relay_python() if network == "allowlist" else ""
    if network == "allowlist" and not (egress_socket and python):
        raise RuntimeError("Linux 沙箱的出站白名单要有出站代理的套接字，以及一个沙箱里能运行的 Python 解释器（/tmp、/run 下的看不到）")
    ws = os.path.realpath(workspace)
    # Unix 套接字没有按连接拒绝的规则，只读挂载也挡不住 connect，只能让它们不出现在沙箱里：
    # /run 里有 docker.sock、D-Bus、XDG_RUNTIME_DIR，/tmp 里有 X11、tmux、ssh-agent
    argv = [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--unshare-pid", "--unshare-ipc", "--die-with-parent", "--new-session", "--tmpfs", "/run"]
    if os.path.isdir("/var/run") and not os.path.islink("/var/run"):
        argv += ["--tmpfs", "/var/run"]
    if network != "allow":   # 新网络命名空间只剩回环，抽象命名空间里的套接字也隔开了
        argv.append("--unshare-net")
    else:   # systemd-resolved 等把 resolv.conf 链接到 /run 下：DNS 配置挂回来
        resolv = os.path.dirname(os.path.realpath("/etc/resolv.conf"))
        if resolv.startswith("/run/"):
            argv += ["--ro-bind", resolv, resolv]
    argv += ["--bind", private_tmp, "/tmp"] if private_tmp else ["--tmpfs", "/tmp"]
    if os.path.isdir("/var/tmp") and not os.path.islink("/var/tmp"):   # 同理不和宿主共用：宿主进程放在那里的套接字看不到
        argv += ["--tmpfs", "/var/tmp"]
    # 挂载有先后：后挂的盖住先挂的。可写的上层目录先挂，工作区其次，再把 harness 状态与凭证位置盖回去
    binds = sorted({os.path.realpath(p) for p in (writable or ()) if os.path.isdir(p)}, key=len)
    for p in binds:
        argv += ["--bind", p, p]
    argv += ["--bind", ws, ws]
    git = os.path.join(ws, ".git")
    if os.path.isdir(git) and not os.path.islink(git):   # 挂载点改不了名：整个 .git 换掉就绕过了里面的只读
        argv += ["--bind", git, git]
        hooks = os.path.join(git, "hooks")
        if not os.path.lexists(hooks):   # 不存在的路径没法 ro-bind：盖一层只读的空 tmpfs。bwrap 会在 .git 里留下一个空目录做挂载点
            argv += ["--tmpfs", hooks, "--remount-ro", hooks]
    for p in (os.path.join(ws, ".flywhl"), os.path.join(ws, "flywhl.toml"), *_read_only(ws, protected)):
        if os.path.exists(p) and _under(p, ws):   # 工作区之外的本来就写不了，/tmp 下的这样一挂反而露出来了
            argv += ["--ro-bind", p, p]
    for p in _credential_paths(ws, dict(os.environ) if env is None else env):
        argv += ["--tmpfs", p] if os.path.isdir(p) else ["--ro-bind", "/dev/null", p]
    # 宿主上其余的套接字（家目录里的 Docker Desktop、colima、screen……）逐个盖成 /dev/null。/run、/tmp、/var/tmp 已经整体换掉，
    # 但挂回去的工作区与可写目录可能就在 /tmp 下面，里面的照样要盖
    pinned = {real for _, real in unix_sockets}
    hidden = [h for h in ("/run", "/var/run", "/tmp", "/var/tmp") if not os.path.islink(h)]
    cwd = os.path.realpath(chdir) if chdir else ws
    if any(_under(cwd, h) for h in hidden) and not any(_under(cwd, p) for p in (ws, *binds)):
        argv += ["--ro-bind", cwd, cwd]   # 配置里写的工作目录（MCP 的 cwd）在换掉的目录下
    for p in _visible_sockets(host_sockets(), hidden, [ws, *binds, cwd]):
        if p not in pinned:
            argv += ["--ro-bind", "/dev/null", p]
    # 显式放行的挂回来：只挂真的套接字；沙箱能写的目录里的不挂
    _refuse_writable([real for _, real in unix_sockets], [ws, *binds, *([os.path.realpath(private_tmp)] if private_tmp else [])])
    for want, real in unix_sockets:
        if _is_socket(real):   # 服务没启动时不挂，连接照常失败
            argv += ["--bind", real, want]
    program = _program(command)
    if network == "allowlist":
        ports = ",".join(p for p in local_ports if p.isdigit() and int(p) != EGRESS_PORT)
        argv += ["--bind", str(egress_socket), EGRESS_SOCKET]
        program = [python, "-I", "-S", "-c", _RELAY, EGRESS_SOCKET, str(EGRESS_PORT), ports, *program]
    return [*argv, "--setenv", "TMPDIR", "/tmp", "--chdir", cwd, *program]


def _is_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.lstat(path).st_mode)
    except OSError:
        return False


def _visible_sockets(paths: Iterable[str], hidden: list[str], exposed: list[str]) -> list[str]:
    """``/proc/net/unix`` 里的套接字在沙箱里看得见的那些（``hidden`` 整体换掉了，``exposed`` 是挂回去的目录）。
    那里记的是绑定时写的路径，不一定是文件现在的位置：
    - 路径经过符号链接（``/tmp/x`` 指向家目录里的某处）时，按目录部分解析后的真实位置判断、遮住；
    - 之后改了名（先绑临时名再改名是常见写法），原路径就不存在了，这时把它所在目录里的套接字都找出来；
      目录本身也被挪走的找不到。目录里条目多到数不完时拒绝执行。"""
    found, scanned = set(), set()
    for p in paths:
        p = os.path.join(os.path.realpath(os.path.dirname(p)), os.path.basename(p))
        if any(_under(p, h) for h in hidden) and not any(_under(p, e) for e in exposed):
            continue
        if _is_socket(p):
            found.add(p)
            continue
        d = os.path.dirname(p)
        if d in scanned:
            continue
        scanned.add(d)
        try:
            names = os.listdir(d)
        except OSError:   # 目录也不在了
            continue
        if len(names) > _SCAN_LIMIT:
            raise RuntimeError(f"宿主进程在 {d} 里绑定的套接字改过名，目录里的条目超过 {_SCAN_LIMIT} 个，没法逐个确认都已遮住")
        found.update(q for q in (os.path.join(d, n) for n in names) if _is_socket(q))
    return sorted(found)


__all__ = ["Detected", "EGRESS_PORT", "EGRESS_SOCKET", "SANDBOX_EXEC", "SANDBOX_LEVELS", "SANDBOX_NETWORK", "bwrap_argv",
           "default_writable", "detect", "probe", "require_bwrap", "sandbox_argv", "host_sockets", "pin_sockets", "seatbelt_profile",
           "workspace_credentials"]
