"""审批疲劳：人开始不看就点批准，人审就形同虚设。两个信号，只看人做出的决定（自动放行、超时、取消不算）：

- **秒批**：从发起审批到批准不到 ``FAST_SECONDS`` 秒。单次秒批说明不了什么（命令很短，一眼就看完了）；本会话
  最近 ``WINDOW`` 次人工决定里有 ``FAST_STREAK`` 次秒批才提示，之后至少再过 ``WINDOW`` 次决定才会再提示——
  提示本身太频繁，同样会被习惯性忽略。
- **同类重复批准**：同一个“本会话始终允许”的键（``grant_for()`` 生成的规则）在本会话被人工批准了 ``REPEAT`` 次，
  每个键提示一次，并给出那条规则。灾难性（critical）的操作不提示：它们在任何授权下都要问。

提示作为 ``notice`` 事件（code=approval_fatigue）进入事件流，时间线与终端都能看到。按会话的计数只在内存里，
重启后重新开始；跨会话的统计（批准率、秒批比例、等待时长分位、重复最多的键）由 ``approval_stats()``
从审批记录现算。
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from ..runtime.store import ApprovalRecord

FAST_SECONDS = 2.0
WINDOW = 10
FAST_STREAK = 3
REPEAT = 3


def is_human(by: str | None) -> bool:
    """人做出的决定。``policy:…`` 是无人值守的自动决定，``system…`` 是超时、取消与交互失败。"""
    return bool(by) and not by.startswith(("policy:", "system"))   # type: ignore[union-attr]


@dataclass
class _Session:
    recent: deque[bool] = field(default_factory=lambda: deque(maxlen=WINDOW))   # 最近的人工决定是不是秒批
    since_warned: int = WINDOW
    approved: Counter[str] = field(default_factory=Counter)
    warned: set[str] = field(default_factory=set)


class FatigueMonitor:
    """按会话记下最近的人工审批，出现疲劳信号时返回提示。"""

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}

    def approved_before(self, session_id: str, key: str) -> int:
        """本会话人工批准过几次同类操作：审批卡片上与“本会话始终允许”的规则一起展示。"""
        s = self._sessions.get(session_id)
        return s.approved[key] if s else 0

    def observe(self, session_id: str, *, approved: bool, by: str, waited: float, key: str, risk: str,
                remembered: bool) -> list[str]:
        if not is_human(by):
            return []
        s = self._sessions.setdefault(session_id, _Session())
        fast = approved and waited < FAST_SECONDS
        s.recent.append(fast)
        s.since_warned += 1
        out = []
        if fast and sum(s.recent) >= FAST_STREAK and s.since_warned >= WINDOW:
            s.since_warned = 0
            out.append(f"最近 {len(s.recent)} 次审批里有 {sum(s.recent)} 次在 {FAST_SECONDS:g} 秒内批准。批准前看一眼命令分析"
                       "与变更预览；总在批准的同类操作改成会话授权或项目规则，把审批留给真正需要判断的动作。")
        if approved and not remembered and risk != "critical":
            s.approved[key] += 1
            if s.approved[key] >= REPEAT and key not in s.warned:
                s.warned.add(key)
                wide = "这是外部副作用操作，授权前确认规则的范围足够窄；" if risk == "high" else ""
                out.append(f"同类操作本会话已人工批准 {s.approved[key]} 次（{key}）。{wide}下次选“本会话始终允许”就不再询问，"
                           "需要长期放行的写进 permissions.rules。")
        return out


def approval_stats(records: Iterable[ApprovalRecord], key_of: Callable[[ApprovalRecord], str | None]) -> dict[str, Any]:
    """审批记录现算的疲劳指标。key_of 给出一条记录的“本会话始终允许”键，算不出时返回 None。"""
    recs = list(records)
    human = [a for a in recs if a.decided_at and is_human(a.decided_by)]
    approved = [a for a in human if a.status == "approved"]
    waits = sorted(a.decided_at - a.created_at for a in human)   # type: ignore[operator]
    fast = [a for a in approved if a.decided_at - a.created_at < FAST_SECONDS]   # type: ignore[operator]
    repeats = Counter(k for a in approved if not a.remember and (k := key_of(a)))

    def pct(q: float) -> float | None:
        return round(waits[min(len(waits) - 1, int(len(waits) * q))], 2) if waits else None

    return {
        "total": len(recs), "pending": sum(a.status == "pending" for a in recs), "human": len(human),
        "approved": len(approved), "rejected": sum(a.status == "rejected" for a in human),
        "timeouts": sum(a.decided_by == "system:timeout" for a in recs),
        "approve_rate": round(len(approved) / len(human), 3) if human else None,
        "fast": len(fast), "fast_rate": round(len(fast) / len(approved), 3) if approved else None,
        "wait_p50": pct(0.5), "wait_p90": pct(0.9),
        "repeated": [{"key": k, "count": n} for k, n in repeats.most_common(5) if n >= REPEAT],
        "fast_seconds": FAST_SECONDS, "repeat_threshold": REPEAT,
    }
