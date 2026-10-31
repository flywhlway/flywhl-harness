"""hook 与 MCP 服务器的供应链检查：它们在 harness 进程外、系统沙箱外运行，是实打实的第三方代码。

两件事：

1. **引用的文件按内容绑定。** ``python3 scripts/guard.py`` 真正执行的是 ``scripts/guard.py``。项目配置的信任哈希
   把这些文件的内容一起算进去，脚本改了就要重新确认；命令 hook 每次执行前再核对一遍——Agent 能改工作区里的文件，
   改过的脚本会在沙箱外、以 harness 的身份运行。只认命令行里直接写出的文件：``python -m 包``、脚本再导入的模块、
   ``cd hooks && ./guard.sh`` 这类要先切换目录的写法都核对不到。
2. **包管理器临时拉取的代码要锁版本。** ``npx -y some-mcp-server`` 每次启动都可能拉到新发布的版本，包被接管、
   维护者账号被盗都会这样投毒。没锁到确切版本的在信任确认、设置页里标出来；``permissions.require_pinned``
   打开时拒绝启动。认得 npx / bunx / ``pnpm dlx`` / ``yarn dlx`` / ``npm exec``、uvx / ``uv tool run`` / ``pipx run``、
   ``docker run`` / ``podman run``（镜像要带 ``@sha256:`` 摘要）。
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
_LOCAL = (".", "/", "~", "file:")


def argv_of(command: str | Iterable[str]) -> list[str]:
    if isinstance(command, str):
        try:
            return shlex.split(command)
        except ValueError:
            return command.split()
    return [str(x) for x in command]


def referenced_files(command: str | Iterable[str], base: str | Path, _depth: int = 0) -> list[Path]:
    """命令里指向现有普通文件的参数（相对路径按 base 解析，跟随符号链接）。PATH 上的命令名不是文件，不算；
    带空白的参数（``sh -c 'python3 guard.py'``）再拆开找一层。"""
    out: list[Path] = []
    for tok in argv_of(command):
        if tok.startswith("-") and "=" in tok:   # --config=hooks/rules.json
            tok = tok.split("=", 1)[1]
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
def unpinned_packages(command: str | Iterable[str], _depth: int = 0) -> list[str]:
    """命令里经包管理器临时拉取、又没锁到确切版本的包，每项一句说明。带空白的参数（``sh -c 'npx x'``）再拆开找一层。"""
    argv = argv_of(command)
    found: list[str] = []
    for i, tok in enumerate(argv):
        name, rest = tok.rsplit("/", 1)[-1], argv[i + 1:]
        if any(c.isspace() for c in tok):
            found += unpinned_packages(tok, _depth + 1) if _depth < 3 else []
            continue
        if name in ("npx", "bunx"):
            npm, specs = True, _npm_specs(rest)
        elif (name, rest[:1]) in (("pnpm", ["dlx"]), ("yarn", ["dlx"]), ("npm", ["exec"]), ("npm", ["x"])):
            npm, name, specs = True, f"{name} {rest[0]}", _npm_specs(rest[1:])
        elif name == "uvx":
            npm, specs = False, _py_specs(rest, spec_opts=("--from", "--with"))
        elif name == "uv" and rest[:2] == ["tool", "run"]:
            npm, name, specs = False, "uv tool run", _py_specs(rest[2:], spec_opts=("--from", "--with"))
        elif name == "pipx" and rest[:1] == ["run"]:
            npm, name, specs = False, "pipx run", _py_specs(rest[1:], spec_opts=("--spec",))
        elif name in ("docker", "podman") and rest[:1] == ["run"]:
            image = _image(rest[1:])
            if image and "@sha256:" not in image:
                found.append(f"{name} 镜像 {image}（没有用 @sha256: 摘要锁定）")
            continue
        else:
            continue
        found += [f"{name} 包 {s}" for s in specs if not _pinned(s, npm=npm)]
    return list(dict.fromkeys(found))


def _npm_specs(args: list[str]) -> list[str]:
    """npx 要拉取的包：``-p`` / ``--package`` 指定的，没有时是第一个位置参数。"""
    packages, i = [], 0
    while i < len(args):
        x = args[i]
        if x in ("-p", "--package") and i + 1 < len(args):
            packages.append(args[i + 1])
            i += 2
            continue
        if x.startswith("--package="):
            packages.append(x.split("=", 1)[1])
        elif x in ("-c", "--call"):
            i += 2
            continue
        elif not x.startswith("-"):
            return packages or [x]
        i += 1
    return packages


def _py_specs(args: list[str], *, spec_opts: tuple[str, ...]) -> list[str]:
    """uvx / pipx run 要装的包：``--from`` / ``--spec`` / ``--with`` 给出的，再加上没被 --from 顶替的第一个位置参数。"""
    takes_value = {"--python", "-p", "--index", "--index-url", "-i", "--extra-index-url", "--default-index", "--constraint",
                   "--constraints", "-c", "--with-editable", "--with-requirements", "--pip-args", "--suffix"}
    specs, has_from, i = [], False, 0
    while i < len(args):
        x = args[i]
        opt, _, val = x.partition("=")
        if opt in spec_opts:
            v = val if "=" in x else (args[i + 1] if i + 1 < len(args) else "")
            specs.append(v)
            has_from = has_from or opt in ("--from", "--spec")
            i += 1 if "=" in x else 2
            continue
        if x.startswith("-"):
            i += 2 if x in takes_value else 1
            continue
        if not has_from:
            specs.append(x)
        break
    return [s for s in specs if s]


def _pinned(spec: str, *, npm: bool) -> bool:
    if spec.startswith(_LOCAL) or _COMMIT.search(spec):
        return True   # 本地路径的内容由引用文件的哈希管；git 地址锁到了提交
    if npm:
        m = re.fullmatch(r"(@[^/@]+/)?[^/@]+@(.+)", spec)
        return bool(m and _SEMVER.fullmatch(m.group(2)))
    m = re.fullmatch(r"[A-Za-z0-9._-]+(?:\[[^\]]*\])?(?:==|@)(.+)", spec)
    return bool(m and re.fullmatch(r"\d[\w.+!-]*", m.group(1)))


# docker run 带一个值的选项（写成 --name=x 的不带）
_DOCKER_VALUE = {"-e", "--env", "--env-file", "-v", "--volume", "--mount", "-p", "--publish", "--name", "-w", "--workdir",
                 "-u", "--user", "--network", "--net", "--entrypoint", "-l", "--label", "--platform", "--pull", "-m",
                 "--memory", "--cpus", "-h", "--hostname", "--add-host", "--device", "--cap-add", "--cap-drop",
                 "--security-opt", "--ulimit", "--tmpfs", "--restart", "--log-driver", "--log-opt", "--shm-size",
                 "--ipc", "--pid", "--runtime", "--gpus", "--dns", "--expose", "--group-add", "--cidfile", "--stop-signal"}


def _image(args: list[str]) -> str:
    i = 0
    while i < len(args):
        x = args[i]
        if x.startswith("-"):
            i += 2 if x in _DOCKER_VALUE else 1
            continue
        return x
    return ""


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
    loose = unpinned_packages(command)
    if loose:
        parts.append(f"没锁版本：{'；'.join(loose)}")
    return "".join(f"；{p}" for p in parts)


def unpinned_in(hook_commands: Iterable[str], mcp_servers: dict) -> list[str]:
    """所有 hook 与 MCP 服务器里没锁版本的包，``permissions.require_pinned`` 打开时据此拒绝启动。"""
    out = [f"hook {c!r}：{p}" for c in hook_commands for p in unpinned_packages(c)]
    out += [f"MCP 服务器 {n}：{p}" for n, s in mcp_servers.items() if isinstance(s, dict)
            for p in unpinned_packages(mcp_argv(s))]
    return out
