"""协作式取消（cooperative cancellation）。

为什么不直接 ``task.cancel()``？
--------------------------------
asyncio 的任务取消是“抢占式”的：它会在任意 ``await`` 处抛出 ``CancelledError``，
可能正好打断一个已经发出去、还没拿到回包的外部写操作（下发指令、发通知、扣款……），
留下“不知道成没成功”的烂摊子。

harness 需要的是 **协作式取消**：取消只是一个“请求”，由令牌沿
``主循环 → 模型流 → 工具 → 子 Agent`` 逐层传播，各层在 **安全点** 自行决定如何停下：

- 模型流：立即断开 HTTP 连接，节省 token；
- 只读工具：立即中止；
- 不可逆工具：不半途打断，等待完成或标记“结果未知”留待对账。

Go 用 ``context.Context``、pi 用 ``AbortSignal``，这里是 Python 的等价物。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, AsyncIterator, Awaitable, TypeVar

T = TypeVar("T")


class RunCancelled(Exception):
    """在安全点检测到取消请求时抛出。携带原因与发起人，用于审计。"""

    def __init__(self, reason: str = "cancelled", by: str = "user"):
        super().__init__(reason)
        self.reason = reason
        self.by = by


class RunPaused(Exception):
    """在安全点检测到暂停请求时抛出：Run 进入 paused，可稍后 resume。"""


class CancelToken:
    """可级联的取消令牌。

    - ``cancel()`` 幂等：只有第一次调用的原因/发起人会被记录；
    - ``child()`` 创建子令牌：父取消会级联到子（子 Agent 随父停止），子取消不影响父；
    - ``guard(awaitable)``：让任意等待可以被取消“打断”；
    - ``iterate(async_iter)``：让异步流可以被取消“打断”，并保证关闭底层流（释放 HTTP 连接）。
    """

    def __init__(self, parent: "CancelToken | None" = None):
        self._event = asyncio.Event()
        self.reason: str | None = None
        self.by: str | None = None
        self._children: list[CancelToken] = []
        if parent is not None:
            parent._children.append(self)
            if parent.cancelled:
                self.cancel(parent.reason or "parent_cancelled", parent.by or "system")

    # ------------------------------------------------------------------ 基本操作
    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self, reason: str = "cancelled", by: str = "user") -> bool:
        """发出取消请求。返回 True 表示这是首次取消。"""
        if self._event.is_set():
            return False
        self.reason, self.by = reason, by
        self._event.set()
        for c in self._children:
            c.cancel(reason, by)
        return True

    def child(self) -> "CancelToken":
        return CancelToken(parent=self)

    def error(self) -> RunCancelled:
        return RunCancelled(self.reason or "cancelled", self.by or "user")

    def raise_if_cancelled(self) -> None:
        """安全点检查：主循环在每个阶段边界调用它。"""
        if self.cancelled:
            raise self.error()

    async def wait(self) -> None:
        await self._event.wait()

    async def sleep(self, seconds: float) -> None:
        """可被取消打断的 sleep（重试退避、轮询等待都应该用它）。"""
        await self.guard(asyncio.sleep(seconds))

    # ------------------------------------------------------------------ 组合
    async def guard(self, aw: Awaitable[T]) -> T:
        """等待 ``aw``，但取消请求到达时立刻放弃并抛出 RunCancelled。

        被放弃的任务会收到 asyncio 取消，因此 ``aw`` 内部应在 finally 中清理资源。
        """
        self.raise_if_cancelled()
        task = asyncio.ensure_future(aw)
        waiter = asyncio.ensure_future(self._event.wait())
        try:
            done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:  # 外层任务被抢占式取消：两边都收拾干净
            task.cancel()
            waiter.cancel()
            raise
        finally:
            waiter.cancel()
        if task in done:
            return task.result()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        raise self.error()

    async def iterate(self, source: AsyncIterator[T]) -> AsyncIterator[T]:
        """包装一个异步迭代器，使其可被取消；无论正常结束、出错还是取消，都会关闭底层流。"""
        it = source.__aiter__()
        try:
            while True:
                try:
                    item = await self.guard(it.__anext__())
                except StopAsyncIteration:
                    return
                yield item
        finally:
            aclose: Any = getattr(it, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()
