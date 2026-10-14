"""端云协同：幂等的指令下发（云端“大脑”→ 端侧“双手”）。

当编排在云端、执行在用户桌面 / 设备 / 远程沙箱时，云与端之间的网络会断、会重复、会乱序。
正确性的基石是 **至少一次投递 + 幂等消费**：

云端（CommandDispatcher）
    - **幂等键去重**：同一幂等键重复提交只生成一条指令（唯一约束）；
    - **ACK 状态机**：``pending → sent → acked | failed | expired``，迁移用条件更新，迟到/重复的 ACK 被忽略；
    - **超时重发**：sent 后超过 ack_timeout 未收到 ACK 就重发（同一指令 ID），超过最大次数判失败；
    - **离线排队与 TTL**：端离线时指令留在队列，上线后按序冲刷；超过 TTL 的指令判过期，不再下发。

端侧（EdgeExecutor）
    - **执行去重表**：已执行过的指令 ID 直接回放上次的结果并重新 ACK，绝不重复执行副作用；
    - 重连后的对账：云端以自己的事件日志为事实源，端侧按指令 ID 回报状态即可收敛。

``FlakyTransport`` 用来模拟丢包、重复与乱序，演示上述机制如何保证“效果上恰好一次”。
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .._utils import new_id, now


class IdempotencyKeyReused(ValueError):
    """同一个幂等键配了不同的指令内容：调用方复用错了键。必须报错——静默返回旧指令会让新内容悄悄丢失。"""


class CmdStatus:
    PENDING = "pending"
    SENT = "sent"
    ACKED = "acked"
    FAILED = "failed"
    EXPIRED = "expired"
    TERMINAL = frozenset({"acked", "failed", "expired"})


@dataclass
class Command:
    id: str
    idem_key: str
    device_id: str
    payload: dict[str, Any]
    status: str = CmdStatus.PENDING
    attempts: int = 0
    created_at: float = field(default_factory=now)
    ttl: float = 300.0
    last_sent_at: float | None = None
    result: Any = None
    error: str | None = None
    history: list[str] = field(default_factory=list)


class CommandDispatcher:
    def __init__(self, transport: "Transport", *, ack_timeout: float = 1.0, max_attempts: int = 6,
                 clock: Callable[[], float] = now):
        self.transport = transport
        self.ack_timeout = ack_timeout
        self.max_attempts = max_attempts
        self.clock = clock
        self.commands: dict[str, Command] = {}
        self._by_key: dict[str, str] = {}    # 幂等键 → 指令 ID（生产中是数据库唯一约束）
        self.online: set[str] = set()

    # ------------------------------------------------------------------ 提交
    def submit(self, device_id: str, payload: dict[str, Any], idem_key: str, ttl: float = 300.0) -> Command:
        if idem_key in self._by_key:
            old = self.commands[self._by_key[idem_key]]
            if (old.device_id, old.payload) != (device_id, payload):
                raise IdempotencyKeyReused(f"幂等键 {idem_key} 已用于另一条指令（{old.id}），拒绝用不同内容提交")
            return old  # 重复提交（内容相同）：返回已有指令，不新建
        cmd = Command(new_id("cmd_"), idem_key, device_id, payload, ttl=ttl, created_at=self.clock())
        cmd.history.append("pending")
        self.commands[cmd.id] = cmd
        self._by_key[idem_key] = cmd.id
        return cmd

    def _move(self, cmd: Command, frm: set[str], to: str) -> bool:
        """条件迁移（CAS 的内存版）：只有处于 frm 中的状态才能迁移，终态不可变。"""
        if cmd.status not in frm:
            return False
        cmd.status = to
        cmd.history.append(to)
        return True

    # ------------------------------------------------------------------ 发送 / 重发 / 过期
    async def pump(self) -> None:
        t = self.clock()
        for cmd in sorted(self.commands.values(), key=lambda c: c.created_at):
            if cmd.status in CmdStatus.TERMINAL:
                continue
            if t - cmd.created_at > cmd.ttl:
                self._move(cmd, {CmdStatus.PENDING, CmdStatus.SENT}, CmdStatus.EXPIRED)
                continue
            if cmd.device_id not in self.online:
                continue  # 离线：留在队列里
            due = cmd.status == CmdStatus.PENDING or (
                cmd.status == CmdStatus.SENT and cmd.last_sent_at is not None and t - cmd.last_sent_at >= self.ack_timeout)
            if not due:
                continue
            if cmd.attempts >= self.max_attempts:
                cmd.error = f"超过最大投递次数 {self.max_attempts}"
                self._move(cmd, {CmdStatus.SENT}, CmdStatus.FAILED)
                continue
            cmd.attempts += 1
            cmd.last_sent_at = t
            if cmd.status == CmdStatus.PENDING:
                self._move(cmd, {CmdStatus.PENDING}, CmdStatus.SENT)
            await self.transport.to_device(cmd.device_id, {"type": "command", "id": cmd.id, "payload": cmd.payload,
                                                           "attempt": cmd.attempts})

    # ------------------------------------------------------------------ ACK
    def on_ack(self, msg: dict[str, Any]) -> bool:
        cmd = self.commands.get(msg.get("id", ""))
        if cmd is None:
            return False  # 未知指令的 ACK：忽略
        ok = msg.get("ok", True)
        moved = self._move(cmd, {CmdStatus.SENT, CmdStatus.PENDING}, CmdStatus.ACKED if ok else CmdStatus.FAILED)
        if moved:
            cmd.result, cmd.error = msg.get("result"), msg.get("error")
        return moved  # False：重复/迟到的 ACK，幂等忽略

    def set_online(self, device_id: str, online: bool) -> None:
        (self.online.add if online else self.online.discard)(device_id)


class EdgeExecutor:
    """端侧执行器：至少一次投递 + 幂等消费。

    去重要覆盖两种重复：**执行完之后** 才到达的重复（查已执行表，重发上次的 ACK），
    以及 **执行过程中** 就到达的重复（云端重试比执行快）——后者等同一个在途任务的结果，而不是再执行一遍。
    """

    def __init__(self, device_id: str, transport: "Transport", handler: Callable[[dict[str, Any]], Awaitable[Any]]):
        self.device_id = device_id
        self.transport = transport
        self.handler = handler
        self.executed: dict[str, dict[str, Any]] = {}   # 指令 ID → ACK（生产中要持久化，重启后依然去重）
        self._inflight: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.side_effects = 0

    async def on_message(self, msg: dict[str, Any]) -> None:
        if msg.get("type") != "command":
            return
        cid = msg["id"]
        if cid in self.executed:  # 重复投递：不再执行，重发上次的 ACK
            await self.transport.to_cloud(self.executed[cid])
            return
        if cid in self._inflight:  # 执行中又收到同一条指令：等那一次的结果
            await self.transport.to_cloud(await asyncio.shield(self._inflight[cid]))
            return
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._inflight[cid] = fut
        try:
            try:
                result = await self.handler(msg["payload"])
                ack = {"type": "ack", "id": cid, "ok": True, "result": result}
                self.side_effects += 1
            except Exception as e:  # noqa: BLE001
                ack = {"type": "ack", "id": cid, "ok": False, "error": str(e)}
            self.executed[cid] = ack
            fut.set_result(ack)
        finally:
            self._inflight.pop(cid, None)
            if not fut.done():
                fut.cancel()
        await self.transport.to_cloud(ack)


class Transport:
    """传输层抽象：云 → 端、端 → 云。生产中可以是 MQTT / WebSocket / HTTP 长轮询。"""

    def __init__(self) -> None:
        self.device: EdgeExecutor | None = None
        self.cloud: CommandDispatcher | None = None

    async def to_device(self, device_id: str, msg: dict[str, Any]) -> None:
        if self.device is not None:
            await self.device.on_message(msg)

    async def to_cloud(self, msg: dict[str, Any]) -> None:
        if self.cloud is not None:
            self.cloud.on_ack(msg)


class FlakyTransport(Transport):
    """不可靠网络：按概率丢包、重复投递、延迟（导致乱序）。"""

    def __init__(self, drop: float = 0.3, dup: float = 0.2, max_delay: float = 0.05, seed: int = 7):
        super().__init__()
        self.drop, self.dup, self.max_delay = drop, dup, max_delay
        self.rng = random.Random(seed)
        self.stats = {"sent": 0, "dropped": 0, "duplicated": 0}
        self._tasks: set[asyncio.Task] = set()

    def _deliver(self, coro_fn: Callable[[], Awaitable[None]]) -> None:
        self.stats["sent"] += 1
        if self.rng.random() < self.drop:
            self.stats["dropped"] += 1
            return
        copies = 2 if self.rng.random() < self.dup else 1
        if copies == 2:
            self.stats["duplicated"] += 1
        for _ in range(copies):
            async def later() -> None:
                await asyncio.sleep(self.rng.random() * self.max_delay)
                await coro_fn()
            t = asyncio.create_task(later())
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)

    async def to_device(self, device_id: str, msg: dict[str, Any]) -> None:
        self._deliver(lambda: Transport.to_device(self, device_id, msg))

    async def to_cloud(self, msg: dict[str, Any]) -> None:
        self._deliver(lambda: Transport.to_cloud(self, msg))

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
