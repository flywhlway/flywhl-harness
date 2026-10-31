"""hook 与 MCP 服务器的供应链检查：它们在 harness 进程外、系统沙箱外运行，是实打实的第三方代码。

两件事：

1. **引用的文件按内容绑定。** ``python3 scripts/guard.py`` 真正执行的是 ``scripts/guard.py``。项目配置的信任哈希
   把这些文件的内容一起算进去，脚本改了就要重新确认；命令 hook 每次执行前再核对一遍——Agent 能改工作区里的文件，
   改过的脚本会在沙箱外、以 harness 的身份运行。只认命令行里直接写出的文件：``python -m 包``、脚本再导入的模块、
   ``cd hooks && ./guard.sh`` 这类要先切换目录的写法都核对不到。
2. **包管理器临时拉取的代码要锁版本。** ``npx -y some-mcp-server`` 每次启动都可能拉到新发布的版本，包被接管、
   维护者账号被盗都会这样投毒。没锁到确切版本的在信任确认、设置页里标出来；``permissions.require_pinned``
   打开时拒绝启动。认得 npx / bunx / ``pnpm dlx`` / ``yarn dlx`` / ``npm exec``、uvx / ``uv tool run`` / ``pipx run``、
   docker / podman / nerdctl 的 ``run``（镜像要带 ``@sha256:`` 摘要）。

   判断“锁没锁”要先找出拉取的是哪个包，而包管理器按自己的选项表解析参数。这里拿不准时一律按没锁处理：
   认不出的选项（分不清下一个词是它的值还是包名）、本地目录（内容没人核对）、compose 文件和依赖清单里的版本。
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
from collections.abc import Iterable
from pathlib import Path

# 系统目录里的程序（解释器、包管理器本身）随系统升级变化，Agent 也改不了，不按内容绑定
_SYSTEM_DIRS = ("/usr/", "/bin/", "/sbin/", "/System/", "/Library/", "/opt/", "/nix/")
_FILE_LIMIT = 32
_SEMVER = re.compile(r"v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")
_COMMIT = re.compile(r"[#@][0-9a-f]{40}$")
_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def argv_of(command: str | Iterable[str]) -> list[str]:
    if isinstance(command, str):
        try:
            return shlex.split(command)
        except ValueError:
            return command.split()
    return [str(x) for x in command]


def _without_scheme(tok: str) -> str:
    """``file:../x.tgz``、``file:///abs/x.whl`` 指的就是本地文件。"""
    return tok.removeprefix("file://") if tok.startswith("file://") else tok.removeprefix("file:")


def referenced_files(command: str | Iterable[str], base: str | Path, _depth: int = 0) -> list[Path]:
    """命令里指向现有普通文件的参数（相对路径按 base 解析，跟随符号链接）。PATH 上的命令名不是文件，不算；
    带空白的参数（``sh -c 'python3 guard.py'``）再拆开找一层。"""
    out: list[Path] = []
    for tok in argv_of(command):
        if tok.startswith("-") and "=" in tok:   # --config=hooks/rules.json
            tok = tok.split("=", 1)[1]
        tok = _without_scheme(tok)
        if not tok or tok.startswith("-") or "\0" in tok:
            continue
        if any(c.isspace() for c in tok):
            if _depth < 3:
                out += [f for f in referenced_files(tok, base, _depth + 1) if f not in out]
            continue
        p = Path(os.path.expanduser(tok))
        p = p if p.is_absolute() else Path(base) / p
        try:
            if not p.is_file():
                continue
            real = p.resolve()
        except OSError:
            continue
        if not str(real).startswith(_SYSTEM_DIRS) and real not in out:
            out.append(real)
        if len(out) >= _FILE_LIMIT:
            break
    return out


def file_digest(path: str | Path) -> str:
    """文件内容的 SHA-256；读不了时返回空串（之后读得了、或者内容变了，都算变化）。"""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def pin_files(files: Iterable[Path]) -> dict[str, str]:
    return {str(p): file_digest(p) for p in files}


def changed_files(pins: dict[str, str]) -> list[str]:
    """记下之后内容变了（或者删了、读不了了）的文件。"""
    return [p for p, d in pins.items() if file_digest(p) != d]


# ---------------------------------------------------------------------- 锁版本
def unpinned_packages(command: str | Iterable[str], base: str | Path = ".", _depth: int = 0) -> list[str]:
    """命令里经包管理器临时拉取、又没锁到确切版本的包，每项一句说明。带空白的参数（``sh -c 'npx x'``）再拆开找一层。
    base 是命令运行的目录，本地路径写法（``./pkg.tgz``）按它找文件。"""
    argv = argv_of(command)
    found: list[str] = []
    for i, tok in enumerate(argv):
        if any(c.isspace() for c in tok):
            found += unpinned_packages(tok, base, _depth + 1) if _depth < 3 else []
        else:
            found += _runner(tok.rsplit("/", 1)[-1], argv[i + 1:], Path(base))
    return list(dict.fromkeys(found))


# 各家的选项表：带值的、不带值的。只收常用的——表里没有的选项写成 --选项=值 也认得出，单独写就按没锁处理
_NPM_VALUES = frozenset({"-p", "--package", "-c", "--call", "--registry", "--cache", "--userconfig", "--prefix",
                         "--loglevel", "-w", "--workspace", "--node-options", "--script-shell", "--tag", "--before"})
_NPM_FLAGS = frozenset({"-y", "--yes", "--no", "-q", "--quiet", "-s", "--silent", "--prefer-offline", "--prefer-online",
                        "--offline", "--ignore-scripts", "--legacy-peer-deps", "--verbose", "--bun"})
_UV_VALUES = frozenset({"--from", "--with", "--with-editable", "--with-requirements", "-p", "--python", "--index",
                        "--default-index", "-i", "--index-url", "--extra-index-url", "-f", "--find-links", "-c",
                        "--constraints", "--overrides", "--cache-dir", "--directory", "--project", "--config-file",
                        "--env-file", "--python-platform", "--resolution", "--prerelease", "--exclude-newer",
                        "--index-strategy", "--keyring-provider", "--link-mode", "--color", "--allow-insecure-host"})
_UV_FLAGS = frozenset({"-q", "--quiet", "-v", "--verbose", "-n", "--no-cache", "--offline", "--isolated", "--native-tls",
                       "--no-config", "--no-progress", "-U", "--upgrade", "--refresh", "--reinstall",
                       "--compile-bytecode", "--no-build", "--no-binary", "--no-sources", "--no-index",
                       "--managed-python", "--no-managed-python", "--no-python-downloads", "--no-env-file"})
_PIPX_VALUES = frozenset({"--spec", "--python", "-i", "--index-url", "--pip-args"})
_PIPX_FLAGS = frozenset({"--no-cache", "--pypackages", "--path", "--system-site-packages", "-e", "--editable",
                         "--fetch-missing-python", "-q", "--quiet", "-v", "--verbose"})
# docker / podman / nerdctl：子命令之前的全局选项，与 run 的选项
_CTR_VALUES = frozenset({"-c", "--context", "-H", "--host", "--config", "-l", "--log-level", "--tlscacert", "--tlscert",
                         "--tlskey", "--url", "--connection", "--root", "--runroot", "--storage-driver", "-n",
                         "--namespace"})
_CTR_FLAGS = frozenset({"-D", "--debug", "--tls", "--tlsverify", "-r", "--remote"})
_RUN_VALUES = frozenset({
    "--add-host", "--annotation", "-a", "--attach", "--blkio-weight", "--blkio-weight-device", "--cap-add", "--cap-drop",
    "--cgroup-parent", "--cgroupns", "--cidfile", "--cpu-count", "--cpu-percent", "--cpu-period", "--cpu-quota",
    "--cpu-rt-period", "--cpu-rt-runtime", "-c", "--cpu-shares", "--cpus", "--cpuset-cpus", "--cpuset-mems",
    "--detach-keys", "--device", "--device-cgroup-rule", "--device-read-bps", "--device-read-iops",
    "--device-write-bps", "--device-write-iops", "--dns", "--dns-option", "--dns-search", "--domainname",
    "--entrypoint", "-e", "--env", "--env-file", "--expose", "--gpus", "--group-add", "--health-cmd",
    "--health-interval", "--health-retries", "--health-start-interval", "--health-start-period", "--health-timeout",
    "-h", "--hostname", "--ip", "--ip6", "--ipc", "--isolation", "--kernel-memory", "-l", "--label", "--label-file",
    "--link", "--link-local-ip", "--log-driver", "--log-opt", "--mac-address", "-m", "--memory",
    "--memory-reservation", "--memory-swap", "--memory-swappiness", "--mount", "--name", "--network", "--net",
    "--network-alias", "--net-alias", "--oom-score-adj", "--pid", "--pids-limit", "--platform", "-p", "--publish",
    "--pull", "--restart", "--runtime", "--security-opt", "--shm-size", "--stop-signal", "--stop-timeout",
    "--storage-opt", "--sysctl", "--tmpfs", "--ulimit", "-u", "--user", "--userns", "--uts", "-v", "--volume",
    "--volume-driver", "--volumes-from", "-w", "--workdir"})
_RUN_FLAGS = frozenset({"-d", "--detach", "--disable-content-trust", "--init", "-i", "--interactive",
                        "--no-healthcheck", "--oom-kill-disable", "--privileged", "-P", "--publish-all", "-q",
                        "--quiet", "--read-only", "--rm", "--sig-proxy", "-t", "--tty"})


def _runner(name: str, rest: list[str], base: Path) -> list[str]:
    if name in ("npx", "bunx"):
        return _npm(name, rest, base, _NPM_VALUES, _NPM_FLAGS)
    if (name, rest[:1]) in (("npm", ["exec"]), ("npm", ["x"]), ("yarn", ["dlx"])):
        return _npm(f"{name} {rest[0]}", rest[1:], base, _NPM_VALUES, _NPM_FLAGS)
    if (name, rest[:1]) == ("pnpm", ["dlx"]):   # pnpm 的 -c 是 --shell-mode，不带值
        return _npm("pnpm dlx", rest[1:], base, _NPM_VALUES - {"-c", "--call"}, _NPM_FLAGS | {"-c", "--shell-mode"})
    if name == "uvx":
        return _python(name, rest, base, _UV_VALUES, _UV_FLAGS, ("--from", "--with", "--with-editable"))
    if name == "uv" and rest[:2] == ["tool", "run"]:
        return _python("uv tool run", rest[2:], base, _UV_VALUES, _UV_FLAGS, ("--from", "--with", "--with-editable"))
    if name == "pipx" and rest[:1] == ["run"]:
        return _python("pipx run", rest[1:], base, _PIPX_VALUES, _PIPX_FLAGS, ("--spec",))
    if name in ("docker", "podman", "nerdctl"):
        return _container(name, rest)
    if name in ("docker-compose", "podman-compose"):
        return [f"{name}（镜像写在 compose 文件里，这里核对不到摘要）"]
    return []


def _scan(args: list[str], values: frozenset[str], flags: frozenset[str]) -> tuple[list[tuple[str, str]], int, str]:
    """按选项表走参数，找第一个操作数（包、镜像、子命令）。返回 (带值的选项, 操作数的下标, 认不出的选项)。

    ``--x=值`` 总能分清；单独的 ``--x`` 两张表里都没有时，分不清下一个词是它的值还是操作数，猜错就会把别的词
    当成要拉取的包（``docker run --某选项 a@sha256:… 真镜像``）。遇到这种选项就停下，由调用方按没锁版本处理。"""
    opts: list[tuple[str, str]] = []
    i = 0
    while i < len(args):
        x, nxt = args[i], (args[i + 1] if i + 1 < len(args) else "")
        if x == "--":
            return opts, i + 1, ""
        if not x.startswith("-") or x == "-":
            return opts, i, ""
        if x.startswith("--"):
            name, eq, val = x.partition("=")
            if eq:
                opts.append((name, val))
            elif name in values:
                opts.append((name, nxt))
                i += 1
            elif name not in flags:
                return opts, len(args), x
            i += 1
            continue
        for j in range(1, len(x)):              # 短选项可以合写：-it、-p8080、-eFOO=1
            o = "-" + x[j]
            if o in flags:
                continue
            if o not in values:
                return opts, len(args), x
            if j + 1 < len(x):
                opts.append((o, x[j + 1:].removeprefix("=")))
            else:
                opts.append((o, nxt))
                i += 1
            break
        i += 1
    return opts, len(args), ""


def _unknown(runner: str, opt: str, what: str = "包") -> str:
    shown = opt if opt.startswith("--") else opt[:2]   # 合写的短选项后面可能直接跟着值（-eTOKEN=…）
    return f"{runner} 的选项 {shown} 认不出，分不清要拉取的是哪个{what}（带值的长选项写成 --选项=值）"


def _npm(runner: str, args: list[str], base: Path, values: frozenset[str], flags: frozenset[str]) -> list[str]:
    """要拉取的包：``-p`` / ``--package`` 指定的，没有时是第一个操作数（``-c`` 只在本地依赖里执行命令）。"""
    opts, i, unknown = _scan(args, values, flags)
    if unknown:
        return [_unknown(runner, unknown)]
    specs = [v for o, v in opts if o in ("-p", "--package")]
    if not specs and i < len(args) and not any(o in ("-c", "--call") for o, _ in opts):
        specs = [args[i]]
    return [d for s in specs if (d := _check(runner, s, base, npm=True))]


def _python(runner: str, args: list[str], base: Path, values: frozenset[str], flags: frozenset[str],
            spec_opts: tuple[str, ...]) -> list[str]:
    """要装的包：spec_opts 给出的，再加上没被 ``--from`` / ``--spec`` 顶替的第一个操作数。"""
    opts, i, unknown = _scan(args, values, flags)
    if unknown:
        return [_unknown(runner, unknown)]
    specs = [v for o, v in opts if o in spec_opts]
    if i < len(args) and not any(o in ("--from", "--spec") for o, _ in opts):
        specs.append(args[i])
    out = [f"{runner} 依赖清单 {_shown(v)}（里面的版本这里核对不到）" for o, v in opts if o == "--with-requirements"]
    return out + [d for s in specs if s and (d := _check(runner, s, base, npm=False))]


def _container(name: str, args: list[str]) -> list[str]:
    _, i, unknown = _scan(args, _CTR_VALUES, _CTR_FLAGS)
    if unknown:
        return [_unknown(name, unknown, "镜像")]
    words = args[i:]
    if words[:1] == ["container"]:
        words = words[1:]
    if words[:1] == ["compose"]:
        return [f"{name} compose（镜像写在 compose 文件里，这里核对不到摘要）"]
    if words[:1] != ["run"]:
        return []
    _, j, unknown = _scan(words[1:], _RUN_VALUES, _RUN_FLAGS)
    if unknown:
        return [_unknown(name, unknown, "镜像")]
    image = words[1 + j] if 1 + j < len(words) else ""
    if not image or _DIGEST.search(image):
        return []           # 没写镜像时 docker 自己会报错
    return [f"{name} 镜像 {image}（没有用 @sha256: 摘要锁定）"]


def _check(runner: str, spec: str, base: Path, *, npm: bool) -> str:
    """没锁住时返回一句说明，锁住了返回空串。"""
    local = _local(spec, base)
    if local is not None:
        return "" if local else f"{runner} 包 {_shown(spec)}（本地目录或不存在的路径，内容没有按内容绑定；打包成文件再引用）"
    if _COMMIT.search(spec):
        return ""           # git 地址锁到了提交
    if npm:
        m = re.fullmatch(r"(@[^/@]+/)?[^/@]+@(.+)", spec)
        ok = bool(m and _SEMVER.fullmatch(m.group(2)))
    else:
        m = re.fullmatch(r"[A-Za-z0-9._-]+(?:\[[^\]]*\])?(?:==|@)(.+)", spec)
        ok = bool(m and re.fullmatch(r"\d[\w.+!-]*", m.group(1)))
    return "" if ok else f"{runner} 包 {_shown(spec)}"


def _local(spec: str, base: Path) -> bool | None:
    """本地路径写法：是普通文件时内容由引用文件的哈希管（True）；是目录或不存在时没人核对内容（False）。
    不是本地路径返回 None。"""
    if not spec.startswith(("file:", ".", "/", "~")):
        return None
    p = Path(os.path.expanduser(_without_scheme(spec)))
    try:
        return (p if p.is_absolute() else base / p).is_file()
    except OSError:
        return False


def _shown(spec: str) -> str:
    """展示用：URL 里的账号密码与查询串常带令牌（``https://user:token@…``、``?access_token=…``），去掉。"""
    spec = re.sub(r"(?<=://)[^/\s]*@", "***@", spec)
    return re.sub(r"\?[^#\s]*", "?***", spec)


# ---------------------------------------------------------------------- 配置层面
def mcp_argv(spec: dict) -> list[str]:
    return [str(spec.get("command") or ""), *[str(x) for x in spec.get("args") or []]]


def mcp_base(spec: dict, base: str | Path) -> Path:
    """MCP 服务器的启动目录：配置了 cwd 时按它（相对路径相对于 base），否则就是 base。"""
    cwd = spec.get("cwd")
    return Path(base) / os.path.expanduser(cwd) if isinstance(cwd, str) and cwd else Path(base)


def config_files(data: dict, base: str | Path) -> list[Path]:
    """一份配置里 hook 与 MCP 服务器引用的文件。项目配置的信任哈希把它们的内容一起算进去。"""
    out: set[Path] = set()
    for h in data.get("hooks") or []:
        if isinstance(h, dict) and isinstance(h.get("command"), str):
            out.update(referenced_files(h["command"], base))
    servers = data.get("mcp_servers")
    for spec in servers.values() if isinstance(servers, dict) else ():
        if isinstance(spec, dict) and isinstance(spec.get("command"), str):
            out.update(referenced_files(mcp_argv(spec), mcp_base(spec, base)))
    return sorted(out)


def describe(command: str | Iterable[str], base: str | Path) -> str:
    """信任确认里补充的说明：引用了哪些文件（内容一并确认）、哪些包没锁版本。"""
    parts = []
    files = referenced_files(command, base)
    if files:
        parts.append(f"引用 {'、'.join(map(str, files))}（文件内容一并确认，改了要重新信任）")
    loose = unpinned_packages(command, base)
    if loose:
        parts.append(f"没锁版本：{'；'.join(loose)}")
    return "".join(f"；{p}" for p in parts)


def unpinned_in(hook_commands: Iterable[str], mcp_servers: dict, base: str | Path) -> list[str]:
    """所有 hook 与 MCP 服务器里没锁版本的包，``permissions.require_pinned`` 打开时据此拒绝启动。"""
    out = [f"hook {c!r}：{p}" for c in hook_commands for p in unpinned_packages(c, base)]
    out += [f"MCP 服务器 {n}：{p}" for n, s in mcp_servers.items() if isinstance(s, dict)
            for p in unpinned_packages(mcp_argv(s), mcp_base(s, base))]
    return out
