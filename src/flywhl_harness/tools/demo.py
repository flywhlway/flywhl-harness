"""演示用工具：一个“外部系统”与两个工具，用来把幂等、对账、取消语义演示清楚。

- ``FakeNotifyService``：模拟一个独立的外部通知系统（单独的 SQLite 文件），**按幂等键去重**；
- ``send_notification``：EXTERNAL 级副作用工具，不可中断、幂等、支持对账；
- ``sleep``：可中断的长耗时工具，用来演示取消在 1 个安全点内生效。

崩溃恢复演示的关键时间线::

    写 intent(started) ──► 外部系统已记录 ──► [kill -9] ──► 回包前进程死亡
                                                      │
    重启 ─► 发现 started 无 result ─► reconcile(幂等键) ─► 外部确认已发送 ─► 补写结果，不重发
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .._utils import now
from .base import Effect, Tool, ToolContext, ToolResult


class IdempotencyConflict(Exception):
    """同一个幂等键配了不同的请求内容：说明调用方复用错了键（或参数被改过），必须报错而不是“返回旧结果”。"""


class FakeNotifyService:
    """模拟外部系统：唯一约束保证同一幂等键只生效一次（服务端去重是幂等的最后一道防线）。

    与 Stripe 等真实 API 一致：重复请求只有在 **内容相同** 时才返回上次的结果；内容不同就是冲突。
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS notifications(idem_key TEXT PRIMARY KEY, recipient TEXT, message TEXT, sent_at REAL)"
            )

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def send(self, idem_key: str, recipient: str, message: str) -> tuple[bool, dict[str, Any]]:
        """返回 (是否本次新发送, 记录)。重复的幂等键直接返回已有记录。"""
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO notifications VALUES(?,?,?,?)", (idem_key, recipient, message, now())
            )
            row = c.execute("SELECT * FROM notifications WHERE idem_key=?", (idem_key,)).fetchone()
        if cur.rowcount == 0 and (row[1], row[2]) != (recipient, message):
            raise IdempotencyConflict(f"幂等键 {idem_key} 已用于另一条通知（发给 {row[1]}），拒绝用不同内容重放")
        return cur.rowcount == 1, {"idem_key": row[0], "recipient": row[1], "message": row[2], "sent_at": row[3]}

    def lookup(self, idem_key: str) -> dict[str, Any] | None:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM notifications WHERE idem_key=?", (idem_key,)).fetchone()
        return {"idem_key": row[0], "recipient": row[1], "message": row[2], "sent_at": row[3]} if row else None

    def count(self) -> int:
        with self._lock, self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]


class SendNotificationTool(Tool):
    name = "send_notification"
    description = "向指定接收人发送一条通知（外部系统，不可撤回）。会携带幂等键，重复调用不会重复发送。"
    parameters = {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "接收人，如 ops-team 或邮箱"},
            "message": {"type": "string", "description": "通知内容"},
        },
        "required": ["to", "message"],
        "additionalProperties": False,
    }
    effect = Effect.EXTERNAL
    interruptible = False   # 已发出的请求不能半途打断
    idempotent = True       # 外部系统按幂等键去重

    def __init__(self, service: FakeNotifyService, latency: float = 1.5):
        self.service = service
        self.latency = latency

    def permission_target(self, args: dict[str, Any]) -> str:
        return str(args.get("to", ""))

    def describe_call(self, args: dict[str, Any]) -> str:
        return f"发送通知给 {args.get('to')}：{str(args.get('message', ''))[:60]}"

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            fresh, rec = self.service.send(ctx.idempotency_key, args["to"], args["message"])
        except IdempotencyConflict as e:
            return ToolResult.error(str(e))
        ctx.emit({"progress": "请求已送达外部系统，等待回包…"})
        await asyncio.sleep(self.latency)  # 模拟网络回包延迟：崩溃演示就发生在这个窗口内
        note = "" if fresh else "（外部系统按幂等键识别为重复请求，未重复发送）"
        return ToolResult(
            content=f"通知已发送给 {args['to']}{note}",
            details={"idempotency_key": ctx.idempotency_key, "fresh": fresh, "record": rec},
        )

    async def reconcile(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult | None:
        rec = self.service.lookup(ctx.idempotency_key)
        if rec is None:
            return None
        if (rec["recipient"], rec["message"]) != (args["to"], args["message"]):
            return ToolResult.error(f"[对账] 幂等键 {ctx.idempotency_key} 对应的已发送内容与本次参数不一致，需要人工核实。",
                                    details={"idempotency_key": ctx.idempotency_key, "record": rec})
        return ToolResult(
            content=f"[对账] 外部系统确认该通知已发送给 {rec['recipient']}，未重复发送。",
            details={"idempotency_key": ctx.idempotency_key, "reconciled": True, "record": rec},
        )


class SleepTool(Tool):
    name = "sleep"
    description = "等待指定秒数（用于等待外部任务完成）。可被取消立即打断。"
    parameters = {
        "type": "object",
        "properties": {"seconds": {"type": "number", "minimum": 0, "maximum": 600, "description": "等待秒数"}},
        "required": ["seconds"],
        "additionalProperties": False,
    }
    effect = Effect.READ
    interruptible = True

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        total = float(args["seconds"])
        step, waited = 0.25, 0.0
        while waited < total:
            await asyncio.sleep(min(step, total - waited))
            waited += step
            ctx.emit({"progress": f"已等待 {min(waited, total):.1f}/{total:.1f} 秒"})
        return ToolResult(content=f"已等待 {total:g} 秒")
