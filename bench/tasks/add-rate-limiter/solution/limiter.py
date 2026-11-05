"""接口限流用的令牌桶。"""

import time


class TokenBucket:
    """令牌桶。

    capacity 是桶的容量，刚创建时桶是满的。令牌按每秒 rate 个连续补充，不会超过 capacity。
    clock 是返回当前秒数的函数。
    """

    def __init__(self, capacity, rate, clock=time.monotonic):
        self.capacity = capacity
        self.rate = rate
        self._clock = clock
        self._tokens = capacity
        self._updated_at = clock()

    def _refill(self):
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated_at) * self.rate)
        self._updated_at = now

    def allow(self, n=1):
        """要 n 个令牌：够用就扣掉并返回 True，不够就不扣并返回 False。"""
        self._refill()
        if self._tokens < n:
            return False
        self._tokens -= n
        return True
