"""hook 与 MCP 服务器的供应链检查：它们在 harness 进程外、系统沙箱外运行，是实打实的第三方代码。

两件事：

1. **引用的文件按内容绑定。** ``python3 scripts/guard.py`` 真正执行的是 ``scripts/guard.py``。项目配置的信任哈希
   把这些文件的内容一起算进去，脚本改了就要重新确认；命令 hook 每次执行前再核对一遍——Agent 能改工作区里的文件，
   改过的脚本会在沙箱外、以 harness 的身份运行。只认命令行里直接写出的文件；换了目录的（``cd hooks && ./guard.sh``、
   ``uv --directory srv run server.py``、``UV_WORKING_DIRECTORY=srv``）在每个可能的目录下都找。``python -m 包``、
   脚本再导入的模块、整个目录形式的项目都核对不到。
2. **包管理器临时拉取的代码要锁版本。** ``npx -y some-mcp-server`` 每次启动都可能拉到新发布的版本，包被接管、
   维护者账号被盗都会这样投毒。没锁到确切版本的在信任确认、设置页里标出来；``permissions.require_pinned``
   打开时拒绝启动。认得 npx / bunx / ``pnpm dlx`` / ``yarn dlx`` / ``npm exec``、uvx / ``uv tool run`` /
   ``uv run --with`` / ``pipx run``、docker / podman / nerdctl 的 ``run``（镜像要带 ``@sha256:`` 摘要）。

   判断“锁没锁”要先找出拉取的是哪个包，而包管理器按自己的选项表解析参数。这里拿不准时一律按没锁处理：
   认不出的选项（分不清下一个词是它的值还是包名）、本地目录与没按内容绑定的本地文件、找不到的脚本、
   compose 文件和依赖清单里的版本。

这两项帮人发现疏忽、发现确认之后的变化，不对抗恶意配置：恶意配置大可以直接 ``curl … | sh``，或者经变量、eval、
另一个脚本间接调用包管理器。信任确认时要看命令本身。
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import NamedTuple

# 系统目录里的程序（解释器、包管理器本身）随系统升级变化，Agent 也改不了，不按内容绑定
_SYSTEM_DIRS = ("/usr/", "/bin/", "/sbin/", "/System/", "/Library/", "/opt/", "/nix/")
_SEMVER = re.compile(r"v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")
_COMMIT = re.compile(r"[#@][0-9a-f]{40}$")
_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
_SCRIPT_LIMIT = 1 << 20


def argv_of(command: str | Iterable[str]) -> list[str]:
    """命令拆成参数。字符串按 shell 的规则拆（hook 经 shell 执行）：``true&&npx x``、``a;npx x`` 里的 npx 一样会运行。
    ``#`` 不当注释：``a#b`` 在 shell 里不是注释，当成注释去掉反而会漏看后面的命令。列表（MCP 的 command 加 args）原样用。"""
    if isinstance(command, str):
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split, lex.commenters = True, ""
        try:
            return list(lex)
        except ValueError:
            return re.findall(r"[^\s;&|()<>]+", command)
    return [str(x) for x in command]


def _without_scheme(tok: str) -> str:
    """``file:../x.tgz``、``file:///abs/x.whl`` 指的就是本地文件。"""
    return tok.removeprefix("file://") if tok.startswith("file://") else tok.removeprefix("file:")


def referenced_files(command: str | Iterable[str], base: str | Path, env: Mapping[str, str] | None = None,
                     _depth: int = 0, _dirs: tuple[Path, ...] = ()) -> list[Path]:
    """命令里指向现有普通文件的参数（相对路径按 base 解析，跟随符号链接）。PATH 上的命令名不是文件，不算；
    带空白的参数（``sh -c 'python3 guard.py'``）再拆开找一层。不设数量上限：漏掉一个，它的内容就没人核对。

    命令换了解析相对路径的目录时（``uv --directory srv run server.py``、``UV_WORKING_DIRECTORY=srv``，见
    ``_run_dirs``），相对路径在每个候选目录下都找一遍。env 是配置里给子进程设的环境变量。"""
    argv = argv_of(command)
    dirs = _bases(Path(base), argv, env, _dirs)
    out: dict[Path, None] = {}
    for tok in argv:
        if any(c.isspace() for c in tok):
            if _depth < 3:
                out.update(dict.fromkeys(referenced_files(tok, base, None, _depth + 1, dirs)))
            continue
        for cand in _path_parts(tok):
            p = Path(os.path.expanduser(cand))
            for q in [p] if p.is_absolute() else [d / p for d in dirs]:
                try:
                    if not q.is_file():
                        continue
                    real = q.resolve()
                except OSError:
                    continue
                if not str(real).startswith(_SYSTEM_DIRS):
                    out[real] = None
    return list(out)


# 会改变“相对路径按哪个目录解析”的写法：uv 的 --directory / --project、pnpm 的 -C / --dir、yarn 的 --cwd、npm 的
# --prefix、env 的 -C / --chdir、shell 的 cd / pushd，以及 uv 的环境变量。不管选项属于哪个程序都算上：多按几个目录找、
# 多绑定几个文件无妨
_DIR_OPTS = ("--directory", "--project", "--dir", "--cwd", "--prefix", "--chdir")
_DIR_VARS = ("UV_WORKING_DIRECTORY", "UV_PROJECT")


def _run_dirs(argv: list[str], env: Mapping[str, str] | None) -> list[str]:
    # 子进程的环境是 harness 自己的（去掉密钥类变量）再叠上配置里的 env
    inherited = {**os.environ, **(env if isinstance(env, Mapping) else {})}
    out = [str(v) for k in _DIR_VARS if (v := inherited.get(k))]
    for i, tok in enumerate(argv):
        name, eq, val = tok.partition("=")
        nxt = argv[i + 1] if i + 1 < len(argv) else ""
        if name in _DIR_OPTS or name in _DIR_VARS and eq:     # 后者是命令前的 VAR=值、env VAR=值
            out.append(val if eq else nxt)
        elif tok.startswith("-C"):
            out.append(tok[2:].removeprefix("=") or nxt)
        elif tok in ("cd", "pushd"):
            out.append(nxt)
    return [d for d in out if d]


def _bases(base: Path, argv: list[str], env: Mapping[str, str] | None, inherited: tuple[Path, ...] = ()) -> tuple[Path, ...]:
    """相对路径可能按哪些目录解析：命令运行的目录，加上命令（或外层命令、配置里的 env）换过去的目录。"""
    moved = (base / os.path.expanduser(d) for d in _run_dirs(argv, env))
    return tuple(dict.fromkeys((base, *inherited, *moved)))


def _path_parts(tok: str) -> list[str]:
    """一个参数里可能指向文件的部分：参数本身；``--config=x`` 的值；``-p./x.tgz``、``-itv./x`` 这类合写短选项
    后面直接跟着的值（不知道哪几个字母是选项，每个位置都试，多绑定几个文件无妨）。"""
    if not tok or "\0" in tok:
        return []
    if tok.startswith("--"):
        return [_without_scheme(tok.split("=", 1)[1])] if "=" in tok else []
    if tok.startswith("-"):
        return [_without_scheme(tok[k:].removeprefix("=")) for k in range(2, len(tok))]
    return [_without_scheme(tok)]


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
class _Where(NamedTuple):
    base: Path                  # 命令运行的目录
    bound: frozenset[Path]      # 内容已经按引用文件绑定的文件
    bases: tuple[Path, ...]     # 相对路径可能按哪些目录解析（见 _bases）


def unpinned_packages(command: str | Iterable[str], base: str | Path = ".",
                      env: Mapping[str, str] | None = None) -> list[str]:
    """命令里经包管理器临时拉取、又没锁到确切版本的包，每项一句说明。带空白的参数（``sh -c 'npx x'``）再拆开找一层。
    env 是配置里给子进程设的环境变量。"""
    argv, root = argv_of(command), Path(base)
    where = _Where(root, frozenset(referenced_files(argv, root, env)), _bases(root, argv, env))
    return list(dict.fromkeys(_unpinned(argv, where, 0)))


def _unpinned(argv: list[str], where: _Where, depth: int) -> list[str]:
    found: list[str] = []
    for i, tok in enumerate(argv):
        if any(c.isspace() for c in tok):
            if depth < 3:
                inner = argv_of(tok)
                found += _unpinned(inner, where._replace(bases=_bases(where.base, inner, None, where.bases)), depth + 1)
        else:
            found += _runner(tok.rsplit("/", 1)[-1], argv[i + 1:], where)
    return found


# 各家的选项表：带值的、不带值的。只收常用的——表里没有的选项写成 --选项=值 也认得出，单独写就按没锁处理
_NPM_VALUES = frozenset({"-p", "--package", "-c", "--call", "--registry", "--cache", "--userconfig", "--prefix",
                         "--loglevel", "-w", "--workspace", "--node-options", "--script-shell", "--tag", "--before",
                         "--cwd"})
_NPM_FLAGS = frozenset({"-y", "--yes", "--no", "-q", "--quiet", "-s", "--silent", "--prefer-offline", "--prefer-online",
                        "--offline", "--ignore-scripts", "--legacy-peer-deps", "--verbose", "--bun"})
_PNPM_VALUES = frozenset({"-C", "--dir", "--filter", "-F", "--reporter", "--loglevel", "--package", "--allow-build"})
_PNPM_FLAGS = frozenset({"-w", "--workspace-root", "-r", "--recursive", "-s", "--silent", "--aggregate-output",
                         "--stream", "--color", "--no-color", "-c", "--shell-mode"})   # pnpm 的 -c 是 --shell-mode，不带值
_UV_VALUES = frozenset({"--from", "--with", "--with-editable", "--with-requirements", "-p", "--python", "--index",
                        "--default-index", "-i", "--index-url", "--extra-index-url", "-f", "--find-links", "-c",
                        "--constraints", "--overrides", "--cache-dir", "--directory", "--project", "--config-file",
                        "--env-file", "--python-platform", "--resolution", "--prerelease", "--exclude-newer",
                        "--index-strategy", "--keyring-provider", "--link-mode", "--color", "--allow-insecure-host",
                        "--python-preference", "--package", "--extra", "--group", "--only-group", "--no-group"})
_UV_FLAGS = frozenset({"-q", "--quiet", "-v", "--verbose", "-n", "--no-cache", "--offline", "--isolated", "--native-tls",
                       "--no-config", "--no-progress", "-U", "--upgrade", "--refresh", "--reinstall",
                       "--compile-bytecode", "--no-build", "--no-binary", "--no-sources", "--no-index",
                       "--managed-python", "--no-managed-python", "--no-python-downloads", "--no-env-file",
                       "--all-extras", "--no-dev", "--frozen", "--locked", "--no-project", "-s", "--script", "-m",
                       "--module", "--no-sync", "--active", "--exact", "--all-packages"})
_UV_SPECS = ("--from", "--with", "--with-editable")
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


def _runner(name: str, rest: list[str], where: _Where) -> list[str]:
    if name in ("npx", "bunx", "pnpx"):
        return _npm(name, rest, where, _NPM_VALUES, _NPM_FLAGS)
    if name in ("npm", "yarn", "pnpm"):
        values, flags = (_PNPM_VALUES, _PNPM_FLAGS) if name == "pnpm" else (_NPM_VALUES, _NPM_FLAGS)
        i = _subcommand(name, rest, values, flags, ("exec", "x") if name == "npm" else ("dlx",))
        return i if isinstance(i, list) else _npm(f"{name} {rest[i]}", rest[i + 1:], where, values, flags)
    if name == "uvx":
        return _python(name, rest, where, _UV_VALUES, _UV_FLAGS, _UV_SPECS)
    if name == "uv":
        i = _subcommand(name, rest, _UV_VALUES, _UV_FLAGS, ("tool", "run"))
        if isinstance(i, list):
            return i
        if rest[i:i + 2] == ["tool", "run"]:
            return _python("uv tool run", rest[i + 2:], where, _UV_VALUES, _UV_FLAGS, _UV_SPECS)
        if rest[i] == "run":    # uv run --with x 命令：操作数是要运行的命令或脚本，不是包
            return _python("uv run", rest[i + 1:], where, _UV_VALUES, _UV_FLAGS, _UV_SPECS, operand=False,
                           scripts=True)
        return []
    if name == "pipx":
        i = _subcommand(name, rest, _PIPX_VALUES, _PIPX_FLAGS, ("run",))
        return i if isinstance(i, list) else _python("pipx run", rest[i + 1:], where, _PIPX_VALUES, _PIPX_FLAGS,
                                                     ("--spec",), scripts=True)
    if name in ("docker", "podman", "nerdctl"):
        return _container(name, rest)
    if name in ("docker-compose", "podman-compose"):
        return [f"{name}（镜像写在 compose 文件里，这里核对不到摘要）"]
    return []


def _subcommand(name: str, rest: list[str], values: frozenset[str], flags: frozenset[str],
                subs: tuple[str, ...], what: str = "包") -> int | list[str]:
    """跳过前端程序（npm、uv、pipx、docker…）子命令之前的全局选项（``npm --registry x exec 包``），返回子命令的下标；
    不是会拉取代码的子命令时返回空列表。全局选项认不出、后面又出现了这类子命令时，分不清那个词是不是选项的值，
    返回一句“认不出”。"""
    _, i, unknown = _scan(rest, values, flags)
    if unknown:
        return [_unknown(name, unknown, what)] if any(w in subs for w in rest) else []
    return i if i < len(rest) and rest[i] in subs else []


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


def _npm(runner: str, args: list[str], where: _Where, values: frozenset[str], flags: frozenset[str]) -> list[str]:
    """要拉取的包：``-p`` / ``--package`` 指定的，没有时是第一个操作数（``-c`` 只在本地依赖里执行命令）。"""
    opts, i, unknown = _scan(args, values, flags)
    if unknown:
        return [_unknown(runner, unknown)]
    specs = [v for o, v in opts if o in ("-p", "--package")]
    if not specs and i < len(args) and not any(o in ("-c", "--call") for o, _ in opts):
        specs = [args[i]]
    return [d for s in specs if (d := _check(runner, s, where, npm=True))]


def _python(runner: str, args: list[str], where: _Where, values: frozenset[str], flags: frozenset[str],
            spec_opts: tuple[str, ...], *, operand: bool = True, scripts: bool = False) -> list[str]:
    """要装的包：spec_opts 给出的，再加上没被 ``--from`` / ``--spec`` 顶替的第一个操作数。scripts：操作数也可能是
    直接运行的脚本（uv run、pipx run 认本地 .py 文件与 URL），这时它不是包。"""
    opts, i, unknown = _scan(args, values, flags)
    if unknown:
        return [_unknown(runner, unknown)]
    specs = [v for o, v in opts if o in spec_opts]
    target = args[i] if i < len(args) else ""
    # pipx run 把 URL 与存在的本地文件当脚本，其余是包名；uv run 的操作数是命令或脚本，.py、--script、存在的路径
    # 都按脚本查（没有扩展名的文件经 shebang 里的 uv run --script 运行，同样会现装内联依赖）
    script = bool(scripts and target and (_URL.match(target) or any(map(_exists, _candidates(target, where))) or not operand
                                          and (target.endswith((".py", ".pyw")) or any(a in ("-s", "--script") for a in args[:i]))))
    if operand and target and not script and not any(o in ("--from", "--spec") for o, _ in opts):
        specs.append(target)
    out = [f"{runner} 依赖清单 {_shown(v)}（里面的版本这里核对不到）" for o, v in opts if o == "--with-requirements"]
    if script:
        out += _script(runner, target, where)
    return out + [d for s in specs if s and (d := _check(runner, s, where, npm=False))]


def _candidates(spec: str, where: _Where) -> list[Path]:
    """本地路径可能指向的文件：绝对路径就是它自己，相对路径在每个候选目录下各算一个。"""
    p = Path(os.path.expanduser(_without_scheme(spec)))
    return [p] if p.is_absolute() else [d / p for d in where.bases]


def _exists(path: Path) -> bool:
    """没权限查看时按存在处理：接下来会读不了，按核对不到报出来。"""
    try:
        return path.exists()
    except OSError:
        return True


def _content_bound(path: Path, where: _Where) -> bool:
    real = path.resolve()
    return real in where.bound or str(real).startswith(_SYSTEM_DIRS)


def _script(runner: str, target: str, where: _Where) -> list[str]:
    """直接运行的脚本：远程 URL 每次都可能拿到不同的内容；本地脚本的内容按引用文件绑定，但 PEP 723 内联依赖
    （``# /// script`` 块里的 dependencies）会在运行前现装，同样要锁版本。换过目录时每个候选位置都查。"""
    if _URL.match(target):
        return [f"{runner} 远程脚本 {_shown(target)}（每次运行都可能拿到不同的内容）"]
    found = [c for c in _candidates(target, where) if _exists(c)]
    if not found:     # uv 会去 PATH 上找同名命令；先 cd 到别处再运行的也是这样
        return [f"{runner} 脚本 {target} 在运行目录里找不到，内联依赖这里核对不到"]
    out: list[str] = []
    for c in found:
        try:
            if c.is_dir():
                out.append(f"{runner} 运行目录 {target}：目录里的代码没有按内容绑定")
            elif not _content_bound(c, where):
                out.append(f"{runner} 脚本 {target} 没有按内容绑定，内联依赖这里核对不到")
            elif c.stat().st_size > _SCRIPT_LIMIT:
                out.append(f"{runner} 脚本 {target} 太大，内联依赖这里核对不到")
            else:
                out += _inline_deps(runner, target, c.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            out.append(f"{runner} 脚本 {target} 读不了，内联依赖这里核对不到")
    return list(dict.fromkeys(out))


def _inline_deps(runner: str, target: str, text: str) -> list[str]:
    """PEP 723 内联依赖，按 uv 的读法解析：只在 ``\\n`` 处分行（去掉行尾 ``\\r``）；块从第一行正好是 ``# /// script``
    的地方开始，之后连续的注释行（``#`` 或 ``# …``）是内容，其中 **最后** 一个 ``///`` 是结束行。
    出现了 ``/// script`` 却对不上这个格式、TOML 解析不了、或者带 ``[tool.uv]``（能换包源、换索引）时，一律算核对不到。"""
    unknown = [f"{runner} 脚本 {target} 的内联依赖解析不了，这里核对不到版本"]
    lines = [x.removesuffix("\r") for x in text.split("\n")]
    if "# /// script" not in lines:
        return unknown if "/// script" in text else []
    body: list[str] = []
    for line in lines[lines.index("# /// script") + 1:]:
        if line != "#" and not line.startswith("# "):
            break
        body.append(line[2:])
    if "///" not in body:
        return unknown
    body = body[:len(body) - 1 - body[::-1].index("///")]
    try:
        meta = tomllib.loads("\n".join(body))
    except tomllib.TOMLDecodeError:
        return unknown
    deps = meta.get("dependencies", [])
    if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
        return unknown
    tool = meta.get("tool")
    out = [f"{runner} 脚本 {target} 的内联元数据带 [tool.uv]（能换包源、换索引），依赖从哪里来这里核对不到"] \
        if isinstance(tool, dict) and "uv" in tool else []
    return out + [f"{runner} 脚本 {target} 的内联依赖 {_shown(d.strip())}" for d in deps
                  if not _version_pinned(re.sub(r"\s+", "", d.split(";")[0]), npm=False)]


def _container(name: str, args: list[str]) -> list[str]:
    i = _subcommand(name, args, _CTR_VALUES, _CTR_FLAGS, ("run", "container", "compose"), "镜像")
    if isinstance(i, list):
        return i
    words = args[i + 1:] if args[i] == "container" else args[i:]
    if args[i] == "compose":
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


def _check(runner: str, spec: str, where: _Where, *, npm: bool) -> str:
    """没锁住时返回一句说明，锁住了返回空串。"""
    local = _local(spec, where)
    if local is not None:
        return "" if local else f"{runner} 包 {_shown(spec)}（本地目录、不存在的路径或没按内容绑定的文件；打包成文件再引用）"
    return "" if _version_pinned(spec, npm=npm) else f"{runner} 包 {_shown(spec)}"


def _version_pinned(spec: str, *, npm: bool) -> bool:
    if _COMMIT.search(spec):
        return True         # git 地址锁到了提交
    if npm:
        m = re.fullmatch(r"(@[^/@]+/)?[^/@]+@(.+)", spec)
        return bool(m and _SEMVER.fullmatch(m.group(2)))
    m = re.fullmatch(r"[A-Za-z0-9._-]+(?:\[[^\]]*\])?(?:==|@)(.+)", spec)
    return bool(m and re.fullmatch(r"\d[\w.+!-]*", m.group(1)))


def _local(spec: str, where: _Where) -> bool | None:
    """本地路径写法：每个可能的位置上存在的都是内容已绑定的文件（或在系统目录里）时算锁住；有目录、有没绑定的文件、
    或者哪里都不存在时不算。不是本地路径返回 None。直接查绑定集合，而不是重新判断“是不是文件”：两处解析方式
    稍有出入，就会出现算作锁住、内容却没人核对的文件。"""
    if not spec.startswith(("file:", ".", "/", "~")):
        return None
    try:
        found = [c for c in _candidates(spec, where) if _exists(c)]
        return bool(found) and all(c.is_file() and _content_bound(c, where) for c in found)
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
            out.update(referenced_files(h["command"], base, h.get("env")))
    servers = data.get("mcp_servers")
    for spec in servers.values() if isinstance(servers, dict) else ():
        if isinstance(spec, dict) and isinstance(spec.get("command"), str):
            out.update(referenced_files(mcp_argv(spec), mcp_base(spec, base), spec.get("env")))
    return sorted(out)


def describe(command: str | Iterable[str], base: str | Path, env: Mapping[str, str] | None = None) -> str:
    """信任确认里补充的说明：引用了哪些文件（内容一并确认）、哪些包没锁版本。"""
    parts = []
    files = referenced_files(command, base, env)
    if files:
        parts.append(f"引用 {'、'.join(map(str, files))}（文件内容一并确认，改了要重新信任）")
    loose = unpinned_packages(command, base, env)
    if loose:
        parts.append(f"没锁版本：{'；'.join(loose)}")
    return "".join(f"；{p}" for p in parts)


def unpinned_in(hooks: Iterable[tuple[str, Mapping[str, str]]], mcp_servers: dict, base: str | Path) -> list[str]:
    """所有 hook（命令与 env）与 MCP 服务器里没锁版本的包，``permissions.require_pinned`` 打开时据此拒绝启动。"""
    out = [f"hook {c!r}：{p}" for c, env in hooks for p in unpinned_packages(c, base, env)]
    out += [f"MCP 服务器 {n}：{p}" for n, s in mcp_servers.items() if isinstance(s, dict)
            for p in unpinned_packages(mcp_argv(s), mcp_base(s, base), s.get("env"))]
    return out
