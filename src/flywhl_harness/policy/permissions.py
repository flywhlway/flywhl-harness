"""权限：模式 + 规则 + 命令分析 + 污点感知。

判定顺序（与 Claude Code 一致）
------------------------------
1. **规则**按 deny → ask → allow 的顺序评估，先命中者生效；任何层级的 deny 都不能被别的层级的 allow 推翻；
2. 没有规则命中时，按 **模式** 的默认姿态决定；
3. 再叠加两条“只升不降”的修正：
   - bash 命令被分析为 dangerous → 至少 ask（bypass 模式除外）；
   - 会话读取过不可信内容（污点）→ 非只读操作的 allow 升级为 ask（对抗提示注入）。

例外：命令分析判定为 **灾难性**（删根目录/家目录、格式化磁盘、写块设备、fork 炸弹）时，
在一切规则与模式之前直接 deny——bypass 模式、会话授权、allow 规则都不能放行。

四种模式
--------
==============  ===================================================
default         只读放行；写文件、执行命令、外部操作都需确认
accept_edits    额外放行工作区内的文件编辑；命令与外部操作仍需确认
plan            只允许只读工具；其它一律拒绝（工具定义仍然保留，只在执行层拒绝）
bypass          全部放行（deny 规则仍然有效）——只应在隔离沙箱中使用
==============  ===================================================

规则语法：``决策:工具(模式)``，模式为 glob，例如::

    allow:bash(git status*)      ask:write(src/**)      deny:read(**/.ssh/**)      deny:send_notification
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable

from ..tools.base import Effect, Tool
from .bash_analyzer import analyze


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
        return any(fnmatch.fnmatchcase(t, self.pattern) for t in targets)

    def __str__(self) -> str:
        return f"{self.decision}:{self.tool}" + (f"({self.pattern})" if self.pattern else "")


# 托管基线：密钥与凭证默认不可读写，最危险的命令直接拒绝
MANAGED_BASELINE = [
    "deny:read(*/.ssh/*)", "deny:read(*id_rsa*)", "deny:read(*id_ed25519*)", "deny:read(*/.aws/credentials)",
    "ask:read(*.env)", "ask:write(*.env)", "ask:edit(*.env)",
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


class PermissionPolicy:
    def __init__(self, mode: str | Mode = Mode.DEFAULT, rules: Iterable[str | Rule] = (), *, workspace: Any = None,
                 baseline: bool = True):
        self.mode = Mode(mode)
        self.workspace = workspace
        parsed = [r if isinstance(r, Rule) else Rule.parse(r) for r in rules]
        self.rules: list[Rule] = ([Rule.parse(r, "managed") for r in MANAGED_BASELINE] if baseline else []) + parsed

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
        effect = tool.effect_for(args)
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
        all_rules = [*self.rules, *grants]

        # 1) 规则：deny → ask → allow，先命中者生效
        decision: Decision | None = None
        reason, hit = "", None
        for want in (Decision.DENY, Decision.ASK, Decision.ALLOW):
            for r in all_rules:
                if r.decision != want:
                    continue
                if tool.name == "bash" and want == Decision.ALLOW and r.pattern:
                    # 复合命令：每一段都必须被 allow 规则覆盖，防止 "git status && rm -rf x" 搭便车
                    segs = targets[1:] or targets[:1]
                    if not all(any(r2.decision == Decision.ALLOW and r2.matches(tool.name, [s]) for r2 in all_rules) for s in segs):
                        continue
                if r.matches(tool.name, targets):
                    decision, hit = want, str(r)
                    reason = f"命中{'托管' if r.source == 'managed' else ('会话授权' if r.source == 'session' else '')}规则 {r}"
                    break
            if decision:
                break

        # 2) 模式默认姿态
        if decision is None:
            decision, reason = self._mode_default(tool, args, effect)

        # 3) 只升不降的修正
        if analysis and analysis.level == "dangerous" and decision == Decision.ALLOW and self.mode != Mode.BYPASS:
            decision, reason = Decision.ASK, "命令分析为高危：" + "；".join(analysis.reasons)
        if analysis and analysis.opaque and decision == Decision.ALLOW and self.mode != Mode.BYPASS:
            # allow 规则（含“本会话总是允许”的授权）按命令前缀匹配：git status "$(rm -rf ~)" 会命中 git status*，
            # 但替换里的命令不受规则约束——这类命令一律交给人确认
            decision, reason = Decision.ASK, "命令包含命令替换或进程替换，其中执行的命令不受 allow 规则约束，需要人工确认"
        if tainted and effect != Effect.READ and decision == Decision.ALLOW and self.mode != Mode.BYPASS:
            decision, reason = Decision.ASK, "会话读取过不可信内容，副作用操作升级为人工确认（防提示注入）"
        if decision == Decision.ASK and analysis and analysis.reasons and analysis.reasons[0] not in reason:
            reason = f"{reason}（{analysis.reasons[0]}）"  # 让审批人一眼看到“为什么要问”
        return PermissionResult(decision, reason, risk, effect, hit, analysis.reasons if analysis else None)

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
    """审批时选择“本会话总是允许”所生成的授权规则：粒度尽量窄。"""
    target = tool.permission_target(args)
    if tool.name == "bash":
        words = target.split()
        prefix = " ".join(words[:2]) if len(words) > 1 else (words[0] if words else "")
        return Rule(Decision.ALLOW, "bash", f"{prefix}*", "session")
    if tool.name in _FILE_TOOLS and target:
        return Rule(Decision.ALLOW, tool.name, target, "session")
    return Rule(Decision.ALLOW, tool.name, None, "session")
