"""权限：模式 + 规则 + 命令分析 + 污点感知。

判定顺序（与 Claude Code 一致）
------------------------------
1. **规则**按 deny → ask → allow 的顺序评估，先命中者生效；任何层级的 deny 都不能被别的层级的 allow 推翻；
2. 没有规则命中时，按 **模式** 的默认姿态决定；
3. 再叠加两条“只升不降”的修正：
   - bash 命令被分析为 dangerous → 至少 ask（bypass 模式除外）；
   - 会话读取过不可信内容（污点）→ 非只读操作的 allow 升级为 ask（对抗提示注入）。

例外：命令分析判定为 **灾难性**（删根目录/家目录、格式化磁盘、写块设备、fork 炸弹）或 **碰到凭证位置** 时，
在一切规则与模式之前直接 deny——bypass 模式、会话授权、allow 规则都不能放行。

无人值守（``on_ask="deny"``）时，判定为 ask 的调用默认拒绝；``auto_approve`` 规则命中的改为自动批准，
见 ``PermissionPolicy.auto_approval``。

人工门：工具的 ``human_gate()`` 有说明时（目前只有 bash 的 ``outside_sandbox``），调用不论走到哪一步，只要没被拒绝，最后都变成
ask，bypass 模式也一样，并标上 ``human_only``：规则、会话授权、``auto_approve`` 都放行不了，无人值守时执行层直接拒绝。
系统沙箱是不依赖判定的最后一道防线，能拆掉它的决定只能由人当场做。

bash 与凭证位置
---------------
文件工具对凭证位置（``policy/sensitive.py`` 的 ``CREDENTIAL_GLOBS``）在任何模式下都拒绝；bash 早期只把命中升为 dangerous，
default 模式下要确认，bypass 模式下 ``cat ~/.kube/config`` 照读。现在 bash 命中凭证档同样直接拒绝，理由：

- **没人看的路径不止 bypass**：``--yes`` / ``on_ask="allow"`` 会自动批准所有 ask；有人看时，``cat ~/.kube/config``
  也像是在排查 kubectl，很容易被顺手批准。凭证一旦进了上下文，就留在事件流、轨迹和日志里，快照回滚撤不回来；
- **只在 bypass 下拒绝说不通**：bypass 是最宽松的模式，单独在这里更严，同一条命令在 default 下却能批准执行；
- **同一个位置，各通道结论一致**：read / grep / write / edit / bash 对凭证位置给出同一个答案。

代价是误报没有“批准后执行”的出路，所以只拒绝凭证档，并尽量收窄匹配（目录按完整路径段匹配，见 ``CREDENTIAL_HINTS``）。
被误伤的三类命令和出路：命令里只是提到这些字样（``grep '\\.kube/' src``、``git commit -m '... .ssh ...'``）——用 grep 工具
（它只检查路径、不检查搜索词）或 ``git commit -F``；显式使用凭证（``ssh -i ~/.ssh/k``、``kubectl --kubeconfig ~/.kube/x``）——
改用默认位置或 ``~/.ssh/config`` 里的 Host 别名，命令里就不必出现路径；管理凭证（``ssh-keygen``、``chmod 600 ~/.ssh/config``）——
由用户自己执行。关掉托管基线（``baseline=False``）时这条拒绝一并关掉，退回“高危要确认”。

线索档（``.env``、``credentials`` 字样、shell 历史）维持高危要确认，bypass 下不升级：
``cp .env.example .env``、``pytest tests/test_credentials.py`` 都会命中，硬拒绝会误伤日常命令。

字符串匹配挡得住顺手的读取和引号拼接、花括号展开这类简单变形，挡不住刻意混淆（运行时才拼出来的
``D=$(echo $HOME/.ku); cat ${D}be/config``、glob、编码）。后者要靠系统沙箱：
默认开启的 ``permissions.sandbox`` 让 bash 跑在 sandbox-exec / bubblewrap 里，由内核按真实路径拒绝。

四种模式
--------
==============  ===================================================
default         只读放行；写文件、执行命令、外部操作都需确认
accept_edits    额外放行工作区内的文件编辑；命令与外部操作仍需确认
plan            只允许只读工具；其它一律拒绝（工具定义仍然保留，只在执行层拒绝）
bypass          全部放行（deny 规则、灾难性命令、凭证位置仍然拒绝）——只应在隔离沙箱中使用
==============  ===================================================

规则语法：``决策:工具(模式)``，模式为 glob，例如::

    allow:bash(git status*)      ask:write(src/**)      deny:read(**/.ssh/**)      deny:send_notification
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from ..tools.base import Effect, Tool
from .bash_analyzer import _ASSIGN, RESERVED, WRAPPERS, analyze
from .sensitive import CREDENTIAL_GLOBS, SECRET_HINT_GLOBS


class Decision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


_DRANK = {Decision.ALLOW: 0, Decision.ASK: 1, Decision.DENY: 2}


def strictest(*ds: Decision | str | None) -> Decision | None:
    vals = [Decision(d) for d in ds if d]
    return max(vals, key=lambda d: _DRANK[d]) if vals else None


class Mode(StrEnum):
    DEFAULT = "default"
    ACCEPT_EDITS = "accept_edits"
    PLAN = "plan"
    BYPASS = "bypass"


_RULE_RE = re.compile(r"^\s*(allow|ask|deny)\s*:\s*([\w*?\-\[\]]+)\s*(?:\((.*)\))?\s*$")


@dataclass(frozen=True)
class Rule:
    decision: Decision
    tool: str
    pattern: str | None = None
    source: str = "user"  # managed（企业托管，不可覆盖）| project | user | session（审批时“本会话总是允许”）

    @classmethod
    def parse(cls, text: str, source: str = "user") -> "Rule":
        m = _RULE_RE.match(text)
        if not m:
            raise ValueError(f"无法解析权限规则：{text!r}（格式：allow|ask|deny:工具(模式)）")
        return cls(Decision(m.group(1)), m.group(2), m.group(3) or None, source)

    def matches(self, tool: str, targets: Iterable[str]) -> bool:
        if not fnmatch.fnmatchcase(tool, self.tool):
            return False
        if self.pattern is None:
            return True
        if self.decision == Decision.ALLOW:
            return any(fnmatch.fnmatchcase(t, self.pattern) for t in targets)
        # deny / ask 不区分大小写：macOS、Windows 的默认文件系统不区分大小写，deny:read(*/.ssh/*) 按原样匹配，
        # 读 .SSH/config 就绕过去了。收紧方向的规则宁可多命中；放行方向的 allow 仍按原样匹配，不因此变宽
        pattern = self.pattern.lower()
        return any(fnmatch.fnmatchcase(t.lower(), pattern) for t in targets)

    def __str__(self) -> str:
        return f"{self.decision}:{self.tool}" + (f"({self.pattern})" if self.pattern else "")


# 托管基线（排在所有规则与模式之前，bypass 也不例外）：密钥与凭证默认不可读写，最危险的命令直接拒绝；
# harness 自己的数据目录与项目配置不能被 Agent 改写——.flywhl/ 里是会话库、审批记录、快照与 .flywhl/config.toml，
# flywhl.toml 会在下次启动时被加载（可以定义命令 hook）。放任它们被改写，等于让 Agent 跨重启给自己提权；
# .git/ 同理：写进 .git/config 的 core.fsmonitor 会在下一次 git status 时执行
# 敏感路径清单与 bash 命令分析共用（policy/sensitive.py）；grep 会把匹配行原样带回上下文，和 read 一样要拦
_CONTENT_TOOLS = ("read", "grep", "write", "edit")
MANAGED_BASELINE = [
    *(f"deny:{t}({g})" for g in CREDENTIAL_GLOBS for t in _CONTENT_TOOLS),
    *(f"ask:{t}({g})" for g in SECRET_HINT_GLOBS for t in _CONTENT_TOOLS),
    "deny:write(.flywhl/*)", "deny:edit(.flywhl/*)", "deny:write(*/.flywhl/*)", "deny:edit(*/.flywhl/*)",
    "ask:write(flywhl.toml)", "ask:edit(flywhl.toml)", "ask:write(*/flywhl.toml)", "ask:edit(*/flywhl.toml)",
    # .git/ 里的 config 与 hooks 会被之后的 git 命令执行（core.fsmonitor、pre-commit……）：accept_edits 也不能自动放行
    *(f"ask:{t}({g})" for g in (".git", "*/.git", ".git/*", "*/.git/*") for t in ("write", "edit")),
    "ask:bash(*.flywhl*)",
    "deny:bash(rm -rf /)", "deny:bash(rm -rf /*)", "deny:bash(rm -rf ~*)", "deny:bash(mkfs*)", "deny:bash(:(){*)",
]

_FILE_TOOLS = {"read", "write", "edit", "ls", "grep"}
_RISK = {Effect.READ: "low", Effect.WRITE: "medium", Effect.EXTERNAL: "high"}


@dataclass
class PermissionResult:
    decision: Decision
    reason: str
    risk: str = "low"
    effect: Effect = Effect.READ
    rule: str | None = None
    analysis: list[str] | None = None
    source: str | None = None       # 命中规则的来源（managed、session……），没命中规则时为 None
    human_only: bool = False        # 过了人工门：只有人当场确认才能执行


def auto_approve_rule(text: str) -> Rule:
    """无人值守时自动批准的规则：写成 ``工具(模式)``（如 ``bash(npm test*)``、``write(src/**)``），可以带 ``allow:`` 前缀。"""
    t = text.strip()
    try:
        return Rule.parse(t if t.startswith("allow:") else f"allow:{t}", "auto_approve")
    except ValueError:
        raise ValueError(f"无法解析自动批准规则：{text!r}（格式：工具(模式)，如 bash(npm test*)、write(src/**)）") from None


class PermissionPolicy:
    def __init__(self, mode: str | Mode = Mode.DEFAULT, rules: Iterable[str | Rule] = (), *, workspace: Any = None,
                 baseline: bool = True, auto_approve: Iterable[str | Rule] = ()):
        self.mode = Mode(mode)
        self.workspace = workspace
        self.baseline = baseline
        parsed = [r if isinstance(r, Rule) else Rule.parse(r) for r in rules]
        self.rules: list[Rule] = ([Rule.parse(r, "managed") for r in MANAGED_BASELINE] if baseline else []) + parsed
        self.auto_rules = [r if isinstance(r, Rule) else auto_approve_rule(r) for r in auto_approve]

    # ------------------------------------------------------------------ 目标串
    def _targets(self, tool: Tool, args: dict[str, Any]) -> list[str]:
        target = tool.permission_target(args)
        if tool.name == "bash":
            a = analyze(target, self._root())
            return [target, *a.segments]
        if tool.name in _FILE_TOOLS and target and self.workspace is not None:
            try:
                p = self.workspace.resolve(target, allow_outside=True)
                return [target, str(p), self.workspace.rel(p)]
            except Exception:
                return [target]
        return [target]

    def _root(self) -> str | None:
        return str(self.workspace.root) if self.workspace is not None else None

    def _outside(self, tool: Tool, args: dict[str, Any]) -> bool:
        if tool.name not in _FILE_TOOLS or self.workspace is None:
            return False
        target = tool.permission_target(args) or "."
        try:
            return not self.workspace.is_inside(self.workspace.resolve(target, allow_outside=True))
        except Exception:
            return True

    # ------------------------------------------------------------------ 判定
    def evaluate(self, tool: Tool, args: dict[str, Any], *, tainted: bool = False,
                 grants: Iterable[Rule] = ()) -> PermissionResult:
        effect = tool.effect_in(args, self._root())
        targets = self._targets(tool, args)
        analysis = analyze(targets[0], self._root()) if tool.name == "bash" else None
        if analysis is not None:  # bash 的副作用等级以“工作区感知”的命令分析为准
            effect = {"read": Effect.READ, "write": Effect.WRITE}.get(analysis.level, Effect.EXTERNAL)
        risk = _RISK[effect]
        if analysis and analysis.level == "dangerous":
            risk = "critical"
        if analysis and analysis.catastrophic:
            return PermissionResult(Decision.DENY, "灾难性命令，任何模式下都拒绝：" + analysis.catastrophic_reason,
                                    "critical", effect, None, analysis.reasons)
        if analysis and analysis.credential and self.baseline:  # 与文件工具的托管基线同一档、同一结论，见模块注释
            return PermissionResult(Decision.DENY, f"命令涉及凭证位置，任何模式下都拒绝（{analysis.credential_reason}）。"
                                    "只是搜索这段文字请用 grep 工具，它只检查搜索的路径、不检查搜索词；"
                                    "确实要用到凭证的操作（ssh -i、--kubeconfig、ssh-keygen……）请让用户自己执行",
                                    "critical", effect, None, analysis.reasons)
        all_rules = [*self.rules, *grants]

        # 1) 规则：deny → ask → allow，先命中者生效
        decision: Decision | None = None
        reason, hit, source = "", None, None
        for want in (Decision.DENY, Decision.ASK, Decision.ALLOW):
            same = [r for r in all_rules if r.decision == want]
            r = self._allowed_by(tool, targets, same) if want == Decision.ALLOW \
                else next((r for r in same if r.matches(tool.name, targets)), None)
            if r is not None:
                decision, hit, source = want, str(r), r.source
                reason = f"命中{'托管' if r.source == 'managed' else ('会话授权' if r.source == 'session' else '')}规则 {r}"
                break

        # 2) 模式默认姿态
        if decision is None:
            decision, reason = self._mode_default(tool, args, effect)
        elif self.mode == Mode.PLAN and effect != Effect.READ and decision != Decision.DENY:
            # plan 模式是只读的硬上限：allow 规则、会话授权放行不了副作用，托管基线的 ask 也不能变成“批准后执行”——
            # 模型常写 find . -not -path './.flywhl/*' -exec ...，命中 ask:bash(*.flywhl*) 后经自动审批就在规划阶段跑了写操作
            decision, reason = Decision.DENY, f"plan 模式只允许只读操作（{reason}）。请先给出计划，待用户切换到执行模式后再修改。"

        # 3) 只升不降的修正
        if analysis and analysis.level == "dangerous" and decision == Decision.ALLOW and self.mode != Mode.BYPASS:
            decision, reason = Decision.ASK, "命令分析为高危：" + "；".join(analysis.reasons)
        if analysis and analysis.opaque and decision == Decision.ALLOW and self.mode != Mode.BYPASS:
            # allow 规则（含“本会话总是允许”的授权）按命令前缀匹配：git status "$(rm -rf ~)" 会命中 git status*，
            # 但替换里的命令不受规则约束；仓库配置给 git diff 挂的 textconv 同理——这类命令一律交给人确认
            decision, reason = Decision.ASK, "命令实际执行的程序不在命令文本里（命令替换、进程替换或仓库配置），不受 allow 规则约束，需要人工确认"
        if tainted and effect != Effect.READ and decision == Decision.ALLOW and self.mode != Mode.BYPASS:
            decision, reason = Decision.ASK, "会话读取过不可信内容，副作用操作升级为人工确认（防提示注入）"
        if decision == Decision.ASK and analysis and analysis.reasons and analysis.reasons[0] not in reason:
            reason = f"{reason}（{analysis.reasons[0]}）"  # 让审批人一眼看到“为什么要问”
        gate = tool.human_gate(args)
        if gate and self.mode == Mode.PLAN and decision != Decision.DENY:
            decision, reason = Decision.DENY, f"plan 模式只允许只读操作，不能离开沙箱：{gate}"
        human_only = bool(gate) and decision != Decision.DENY
        if human_only:   # 模式、规则、会话授权的放行到这里都作废，见模块注释“人工门”
            why = f"（{reason}）" if decision == Decision.ASK and reason else ""   # 原来为什么要问，审批人同样要看到
            decision, reason, risk = Decision.ASK, f"{gate}{why}", "critical"
        return PermissionResult(decision, reason, risk, effect, hit, analysis.reasons if analysis else None, source, human_only)

    def _allowed_by(self, tool: Tool, targets: list[str], rules: list[Rule]) -> Rule | None:
        """放行这次调用的第一条规则。bash 复合命令每一段都要被其中某条覆盖，防止 "git status && rm -rf x" 搭便车；
        文件工具按实际落点匹配，见 ``_landing``。"""
        targets = self._landing(tool, targets)
        segs = targets[1:] or targets[:1]
        for r in rules:
            if tool.name == "bash" and r.pattern and not all(any(r2.matches(tool.name, [s]) for r2 in rules) for s in segs):
                continue
            if r.matches(tool.name, targets):
                return r
        return None

    def _landing(self, tool: Tool, targets: list[str]) -> list[str]:
        """放行一侧（allow 规则、会话授权、auto_approve）给文件工具匹配的目标串：文件实际落在哪里——解析后的绝对路径与
        工作区相对路径。原始路径只在不含 ``..``、也没经过工作区里的软链接时才算：``src/../../etc/hosts``、
        指向工作区外的 ``src/link/hosts`` 原样能被 ``read(src/**)`` 匹配上，落点却不归这条规则管。
        工作区之外的软链接（macOS 的 ``/tmp``）不是仓库能放进来的，``allow:read(/tmp/**)`` 照常命中。
        deny / ask 是收紧方向，仍对原始路径和落点都匹配，宁可多命中。"""
        if tool.name not in _FILE_TOOLS or self.workspace is None or len(targets) != 3:
            return targets
        raw, resolved, rel = targets
        q = Path(os.path.expanduser(raw))
        q = q if q.is_absolute() else self.workspace.root / q
        if ".." in q.parts or (q.is_relative_to(self.workspace.root) and str(q) != resolved):
            return [resolved, rel]
        return targets

    def auto_approval(self, tool: Tool, args: dict[str, Any], perm: PermissionResult, *,
                      tainted: bool = False) -> tuple[Rule | None, str]:
        """无人值守时，这次需要确认的调用能否按 ``auto_approve`` 规则自动批准：返回 (命中的规则, "")，或者
        (None, 不批准的原因)。规则按 allow 规则的口径匹配，但这几类调用始终不自动批准，照旧拒绝：

        - 命令分析为高危的（risk = critical）；
        - 实际执行的程序不在命令文本里的（命令替换、仓库配置）：规则按文本匹配，担保不了它们；
        - 托管基线要求确认的（密钥线索、.git 内部、flywhl 配置）；
        - 文件工具读写工作区之外的路径：无人值守的运行不出工作区，规则写得再宽也一样；
        - 会话读取过不可信内容之后的外部副作用操作：提示注入要把数据送出去，走的就是这条路。
        """
        if not self.auto_rules:
            return None, ""
        if perm.risk == "critical":
            return None, "高危命令不自动批准"
        if tool.name == "bash" and analyze(tool.permission_target(args), self._root()).opaque:
            return None, "命令实际执行的程序不在命令文本里，不自动批准"
        if perm.source == "managed":
            return None, f"托管基线要求确认（{perm.rule}），不自动批准"
        if self._outside(tool, args):
            return None, "工作区之外的路径不自动批准"
        if tainted and perm.effect == Effect.EXTERNAL:
            return None, "会话读取过不可信内容，外部副作用操作不自动批准"
        r = self._allowed_by(tool, self._targets(tool, args), self.auto_rules)
        return (r, "") if r else (None, "没有匹配的 auto_approve 规则")

    def _mode_default(self, tool: Tool, args: dict[str, Any], effect: Effect) -> tuple[Decision, str]:
        if self.mode == Mode.BYPASS:
            return Decision.ALLOW, "bypass 模式：全部放行"
        if self.mode == Mode.PLAN:
            if effect == Effect.READ and not self._outside(tool, args):
                return Decision.ALLOW, "plan 模式：只读放行"
            return Decision.DENY, "plan 模式只允许只读操作。请先给出计划，待用户切换到执行模式后再修改。"
        if effect == Effect.READ:
            if self._outside(tool, args):
                return Decision.ASK, "读取工作区之外的路径"
            return Decision.ALLOW, "只读操作"
        if self.mode == Mode.ACCEPT_EDITS and effect == Effect.WRITE and tool.name in ("write", "edit") \
                and not self._outside(tool, args):
            return Decision.ALLOW, "accept_edits 模式：自动接受工作区内的文件编辑"
        return Decision.ASK, {Effect.WRITE: "写操作需要确认", Effect.EXTERNAL: "外部副作用操作需要确认"}[effect]


def grant_for(tool: Tool, args: dict[str, Any]) -> Rule:
    """审批时选择“本会话总是允许”所生成的授权规则：粒度尽量窄。

    bash 默认按前两个词授权（``pytest -q*``）。前两个词说明不了实际执行什么的命令只精确授权这一条：
    ``bash -lc '...'``、``python3 -c '...'``、``find . -exec ...`` 的代码在参数里，``timeout 10 ...``、``FOO=1 ...``
    后面跟的才是真正的命令——按前缀授权，批准一次 ``bash -lc 'echo hi'`` 就等于放行之后所有 ``bash -lc``。
    文件工具按字面授权这一个路径：批准写一个名叫 ``*`` 的文件，不能变成放行之后所有写入。
    """
    target = tool.permission_target(args)
    if tool.name == "bash":
        a = analyze(target)
        words = target.split()
        head = words[0].rsplit("/", 1)[-1] if words else ""
        if a.inline or a.opaque or head in WRAPPERS or head in RESERVED or (words and _ASSIGN.fullmatch(words[0])):
            exact = a.segments[0] if len(a.segments) == 1 else target
            return Rule(Decision.ALLOW, "bash", _glob_literal(exact), "session")
        prefix = " ".join(words[:2]) if len(words) > 1 else (words[0] if words else "")
        return Rule(Decision.ALLOW, "bash", f"{prefix}*", "session")
    if tool.name in _FILE_TOOLS and target:
        return Rule(Decision.ALLOW, tool.name, _glob_literal(target), "session")
    return Rule(Decision.ALLOW, tool.name, None, "session")


def _glob_literal(text: str) -> str:
    """把一段文字转成只匹配它自己的 glob。换行换成 ``?``：授权以 ``allow:bash(...)`` 的文本存进会话元数据，
    Rule.parse 读回时不跨行。"""
    return re.sub(r"[*?\[]", lambda m: f"[{m.group(0)}]", text).replace("\r", "?").replace("\n", "?")
