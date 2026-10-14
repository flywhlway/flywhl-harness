"""重试、退避、熔断与重试预算。

重试的三条纪律
--------------
1. **先分类再决定**：重试同样的请求（限流/过载/超时）、改变输入后重试（上下文超长 → 先压缩）、
   还是不重试（鉴权失败、参数非法）。分类见 ``llm.base.ErrorKind``。
2. **指数退避 + 全抖动（full jitter）**：``delay = uniform(0, min(cap, base·2^attempt))``，
   让大量客户端的重试在时间上打散，避免“惊群”；服务端给了 ``Retry-After`` 就以它为下限。
3. **防重试风暴**：
   - 截止时间沿调用链传递：剩余时间不够就不重试；
   - 熔断器：某个 provider 连续失败就“断开”一段时间，期间直接失败或降级，不再打它；
   - 重试预算：重试量不得超过正常请求量的一定比例（如 20%），故障期间自动收敛；
   - 全链路只有一层负责重试（SDK 的自动重试要关掉）。
"""

from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

from ..cancel import CancelToken

T = TypeVar("T")


@dataclass
class RetryPolicy:
    max_attempts: int = 4          # 含首次尝试
    base_delay: float = 0.5
    max_delay: float = 20.0
    deadline: float | None = None  # 绝对截止时间（time.monotonic），由上层预算传入

    def backoff(self, attempt: int, retry_after: float | None = None, rng: random.Random | None = None) -> float:
        """第 attempt 次重试（从 1 开始）前的等待时间：全抖动。"""
        r = rng or random
        delay = r.uniform(0, min(self.max_delay, self.base_delay * (2 ** (attempt - 1))))
        if retry_after:
            delay = max(delay, retry_after)
        return delay

    def time_left(self) -> float | None:
        return None if self.deadline is None else self.deadline - time.monotonic()


class CircuitOpen(Exception):
    """熔断器处于打开状态：快速失败，交给上层降级。"""


class CircuitBreaker:
    """三态熔断器：closed（正常）→ open（快速失败）→ half_open（放一个探测请求）。

    探测名额必须 **一定会被归还**：探测请求被取消、或以“不计入熔断”的错误（如鉴权失败）结束时，
    调用方要 ``release()``；万一调用方忘了，探测名额也会在 reset_timeout 后过期——
    否则熔断器会永远停在“正在探测”，再也不放行任何请求。
    """

    def __init__(self, failure_threshold: int = 5, reset_timeout: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.clock = clock
        self.failures = 0
        self.opened_at: float | None = None
        self._probing = False
        self._probe_at = 0.0

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        if self.clock() - self.opened_at >= self.reset_timeout:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        s = self.state
        if s == "closed":
            return True
        if s == "half_open" and (not self._probing or self.clock() - self._probe_at >= self.reset_timeout):
            self._probing, self._probe_at = True, self.clock()  # 只放行一个探测请求
            return True
        return False

    def release(self) -> None:
        """结果不说明服务健康与否（取消、非重试类错误）：归还探测名额，状态不变。"""
        self._probing = False

    def success(self) -> None:
        self.failures, self.opened_at, self._probing = 0, None, False

    def failure(self) -> None:
        self._probing = False
        self.failures += 1
        if self.failures >= self.failure_threshold or self.opened_at is not None:
            self.opened_at = self.clock()


class RetryBudget:
    """重试预算：滑动窗口内“重试次数 / 请求次数”不超过 ratio（另有最低保底次数）。"""

    def __init__(self, ratio: float = 0.2, min_retries: int = 3, window: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.ratio, self.min_retries, self.window, self.clock = ratio, min_retries, window, clock
        self._requests: deque[float] = deque()
        self._retries: deque[float] = deque()

    def _trim(self) -> None:
        cutoff = self.clock() - self.window
        for q in (self._requests, self._retries):
            while q and q[0] < cutoff:
                q.popleft()

    def record_request(self) -> None:
        self._requests.append(self.clock())

    def can_retry(self) -> bool:
        self._trim()
        return len(self._retries) < max(self.min_retries, self.ratio * len(self._requests))

    def record_retry(self) -> None:
        self._retries.append(self.clock())


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    is_retryable: Callable[[Exception], bool],
    retry_after: Callable[[Exception], float | None] = lambda e: getattr(e, "retry_after", None),
    cancel: CancelToken | None = None,
    on_retry: Callable[[int, float, Exception], Any] | None = None,
    breaker: CircuitBreaker | None = None,
    budget: RetryBudget | None = None,
) -> T:
    """通用重试器：可重试才重试、尊重截止时间与 Retry-After、可被取消打断。"""
    cancel = cancel or CancelToken()
    attempt = 0
    while True:
        if breaker is not None and not breaker.allow():
            raise CircuitOpen("熔断器打开，快速失败")
        if budget is not None:
            budget.record_request()
        try:
            out = await fn()
            if breaker is not None:
                breaker.success()
            return out
        except BaseException as e:
            if breaker is not None:
                breaker.failure() if isinstance(e, Exception) and is_retryable(e) else breaker.release()
            if not isinstance(e, Exception):
                raise  # 取消等：不重试
            attempt += 1
            if not is_retryable(e) or attempt >= policy.max_attempts:
                raise
            if budget is not None and not budget.can_retry():
                raise
            delay = policy.backoff(attempt, retry_after(e))
            left = policy.time_left()
            if left is not None and left < delay:
                raise  # 剩余时间不足以再试一次：不重试
            if budget is not None:
                budget.record_retry()
            if on_retry is not None:
                on_retry(attempt, delay, e)
            await cancel.sleep(delay)
