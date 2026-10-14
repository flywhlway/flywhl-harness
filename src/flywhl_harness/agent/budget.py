"""预算与循环守卫：终止条件“多条件并联 + 状态如实区分”。

终止的四类来源
--------------
1. 自然完成：没有工具调用，且 Stop 验收通过；
2. 限额：轮次、工具调用次数、token、费用、墙钟时间；
3. 外部：用户取消、审批拒绝；
4. 不可恢复错误。

触发限额时以 ``budget_exhausted`` / ``timed_out`` / ``loop_detected`` 等 **明确状态** 结束，
并（可选地）让模型做一次无工具的总结性收尾——绝不把“被截停”伪装成“成功”。

分层预算：子代理的预算从父预算中切出（``child()``），子代理的消耗会向上累计到父预算，
于是“租户 → 会话 → Run → 子代理”的层级上限天然成立。
"""

from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass, field

from .._utils import canonical_json, short_hash
from ..config import BudgetConfig, LoopGuardConfig
from ..types import Usage


@dataclass
class BudgetVerdict:
    state: str = "ok"              # ok | soft | hard
    reasons: list[str] = field(default_factory=list)
    status: str | None = None      # hard 时对应的终止状态


class Budget:
    def __init__(self, limits: BudgetConfig, parent: "Budget | None" = None, clock=time.monotonic):
        self.limits = limits
        self.parent = parent
        self.clock = clock
        self.started = clock()
        self.turns = 0
        self.tool_calls = 0
        self.usage = Usage()
        self._soft_warned: set[str] = set()

    # ------------------------------------------------------------------ 记账
    def next_turn(self) -> None:
        self.turns += 1

    def charge_model(self, usage: Usage | None) -> None:
        if usage is None:
            return
        self.usage = self.usage + usage
        if self.parent is not None:
            self.parent.charge_model(usage)

    def charge_tool(self, n: int = 1) -> None:
        self.tool_calls += n
        if self.parent is not None:
            self.parent.charge_tool(n)

    def child(self, limits: BudgetConfig | None = None) -> "Budget":
        """为子代理切出预算：不超过父预算的剩余额度。"""
        lim = limits or BudgetConfig(max_turns=20, max_tool_calls=60)
        if self.limits.max_cost_usd is not None:
            remain = max(0.0, self.limits.max_cost_usd - self.usage.cost_usd)
            lim.max_cost_usd = min(lim.max_cost_usd or remain, remain)
        if self.limits.max_total_tokens is not None:
            remain_t = max(0, self.limits.max_total_tokens - self.usage.total_tokens)
            lim.max_total_tokens = min(lim.max_total_tokens or remain_t, remain_t)
        return Budget(lim, parent=self, clock=self.clock)

    @property
    def elapsed(self) -> float:
        return self.clock() - self.started

    # ------------------------------------------------------------------ 判定
    def _ratios(self) -> dict[str, tuple[float, str]]:
        lim = self.limits
        r: dict[str, tuple[float, str]] = {
            "turns": (self.turns / lim.max_turns if lim.max_turns else 0, f"轮次 {self.turns}/{lim.max_turns}"),
            "tool_calls": (self.tool_calls / lim.max_tool_calls if lim.max_tool_calls else 0,
                           f"工具调用 {self.tool_calls}/{lim.max_tool_calls}"),
        }
        if lim.max_total_tokens:
            r["tokens"] = (self.usage.total_tokens / lim.max_total_tokens, f"token {self.usage.total_tokens}/{lim.max_total_tokens}")
        if lim.max_cost_usd:
            r["cost"] = (self.usage.cost_usd / lim.max_cost_usd, f"费用 ${self.usage.cost_usd:.4f}/${lim.max_cost_usd}")
        if lim.max_wall_seconds:
            r["wall"] = (self.elapsed / lim.max_wall_seconds, f"耗时 {self.elapsed:.0f}s/{lim.max_wall_seconds:.0f}s")
        return r

    def hard_verdict(self) -> BudgetVerdict | None:
        """只判断硬阈值（无副作用）。子预算用它检查上层预算，不会“消耗”上层的软提醒。"""
        hard = [(k, d) for k, (r, d) in self._ratios().items() if r >= 1.0]
        if hard:
            status = "timed_out" if any(k == "wall" for k, _ in hard) else "budget_exhausted"
            return BudgetVerdict("hard", [d for _, d in hard], status)
        if self.parent is not None:
            pv = self.parent.hard_verdict()
            if pv is not None:
                return BudgetVerdict("hard", [f"上层预算：{r}" for r in pv.reasons], pv.status)
        return None

    def check(self) -> BudgetVerdict:
        """回合开始前调用：hard 表示必须停止；soft 表示应提醒模型收尾（每种原因只提醒一次）。"""
        hv = self.hard_verdict()
        if hv is not None:
            return hv  # 原因里只列真正超限的维度
        v = BudgetVerdict()
        for key, (ratio, desc) in self._ratios().items():
            if ratio >= self.limits.soft_ratio and key not in self._soft_warned:
                self._soft_warned.add(key)
                v.state = "soft"
                v.reasons.append(desc)
        return v

    def deadline_left(self) -> float | None:
        """剩余墙钟时间：沿调用链传给工具超时，“剩余时间不够就不重试”。子预算取自身与上层的较小值。"""
        own = max(0.0, self.limits.max_wall_seconds - self.elapsed) if self.limits.max_wall_seconds else None
        up = self.parent.deadline_left() if self.parent is not None else None
        vals = [x for x in (own, up) if x is not None]
        return min(vals) if vals else None

    # ------------------------------------------------------------------ 跨暂停 / 崩溃延续
    def state(self) -> dict:
        """可持久化的预算状态：暂停恢复、崩溃接管后从这里继续计数，而不是从零开始（否则反复暂停就能绕过限额）。"""
        return {"turns": self.turns, "tool_calls": self.tool_calls, "usage": self.usage.to_dict(),
                "elapsed": round(self.elapsed, 3)}

    def restore(self, st: dict | None) -> "Budget":
        if st:
            self.turns = int(st.get("turns", 0))
            self.tool_calls = int(st.get("tool_calls", 0))
            self.usage = Usage(**st["usage"]) if st.get("usage") else Usage()
            self.started = self.clock() - float(st.get("elapsed", 0))  # 暂停期间不计入墙钟
        return self

    def snapshot(self) -> dict:
        lim = self.limits
        return {
            "turns": self.turns, "max_turns": lim.max_turns,
            "tool_calls": self.tool_calls, "max_tool_calls": lim.max_tool_calls,
            "usage": self.usage.to_dict(), "max_total_tokens": lim.max_total_tokens,
            "max_cost_usd": lim.max_cost_usd, "elapsed": round(self.elapsed, 2), "max_wall_seconds": lim.max_wall_seconds,
        }


@dataclass
class LoopVerdict:
    action: str = "ok"     # ok | warn | stop
    message: str = ""


class LoopGuard:
    """死循环与无进展检测。

    - **重复调用**：对（工具名 + 规范化参数）签名，再结合结果哈希——“同样的调用得到同样的结果”才算原地打转；
      合理的轮询（等待构建完成）结果会变化，不会被误判。
    - **乒乓模式**：A-B-A-B 交替出现；
    - **连续错误**：连续 N 次工具报错；
    - **同一文件反复编辑**：同一文件被编辑 N 次（LangChain LoopDetectionMiddleware 的做法），提醒换思路。
    """

    def __init__(self, cfg: LoopGuardConfig):
        self.cfg = cfg
        self.recent: deque[tuple[str, str]] = deque(maxlen=cfg.window)
        self.consecutive_errors = 0
        self.edit_counts: Counter[str] = Counter()
        self._warned: set[str] = set()

    @staticmethod
    def signature(name: str, args: dict) -> str:
        return short_hash(name + canonical_json(args))

    def observe(self, name: str, args: dict, result_text: str, is_error: bool) -> LoopVerdict:
        sig = self.signature(name, args)
        self.recent.append((sig, short_hash(result_text)))
        self.consecutive_errors = self.consecutive_errors + 1 if is_error else 0
        if name in ("edit", "write") and not is_error and args.get("path"):
            self.edit_counts[str(args["path"])] += 1

        dup = sum(1 for x in self.recent if x == self.recent[-1])
        n = len(self.recent)
        if dup >= self.cfg.stop_repeats:
            return LoopVerdict("stop", f"最近 {n} 次调用中，{name} 以相同参数得到相同结果 {dup} 次，判定为死循环。")
        if dup >= self.cfg.warn_repeats and f"dup:{sig}" not in self._warned:
            self._warned.add(f"dup:{sig}")
            return LoopVerdict("warn", f"最近 {n} 次调用中，你已经 {dup} 次以相同参数调用 {name} 且结果相同。请换一种策略，或说明阻塞原因。")
        sigs = [s for s, _ in self.recent]
        if len(sigs) >= 6 and sigs[-1] == sigs[-3] == sigs[-5] and sigs[-2] == sigs[-4] == sigs[-6] and sigs[-1] != sigs[-2]:
            if "pingpong" not in self._warned:
                self._warned.add("pingpong")
                return LoopVerdict("warn", "检测到两个调用来回交替（A-B-A-B），似乎没有进展。请重新评估思路。")
        if self.consecutive_errors >= self.cfg.max_consecutive_errors:
            return LoopVerdict("stop", f"工具已连续 {self.consecutive_errors} 次报错。")
        if self.consecutive_errors == max(3, self.cfg.max_consecutive_errors // 2) and "errors" not in self._warned:
            self._warned.add("errors")
            return LoopVerdict("warn", "工具已连续多次报错。请仔细阅读错误信息，换一种方法，不要原样重试。")
        for path, n in self.edit_counts.items():
            if n >= self.cfg.same_file_edit_warn and f"edit:{path}" not in self._warned:
                self._warned.add(f"edit:{path}")
                return LoopVerdict("warn", f"文件 {path} 已被修改 {n} 次。请退一步，重新审视整体方案。")
        return LoopVerdict()
