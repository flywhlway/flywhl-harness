"""弹性模型接入：重试 + 熔断 + 有序降级（fallback 链）。

对流式调用重试有一个特殊之处：**部分输出已经推给了 UI**。处理方式是整体重发请求，
并先发出一个 ``retry`` 事件（``discard_partial=True``），让上层丢弃半截消息（尤其是半截 tool_use），
已产生的 token 成本由 usage 如实记录。

降级链的坑：备用模型的工具调用行为、提示词效果、上下文窗口都可能不同；
每条 assistant 消息都记录了实际服务的 ``provider/model``，便于评测时做归因。
"""

from __future__ import annotations

from dataclasses import replace
from typing import AsyncIterator

from ..cancel import CancelToken
from ..runtime.retry import CircuitBreaker, RetryPolicy
from .base import ErrorKind, ModelRequest, Provider, ProviderError, StreamEvent
from .models import ModelInfo

# 这些错误换一个模型也许能成功；鉴权/上下文超长/参数非法换模型也没用（上下文超长交给 Loop 去压缩）
_FALLBACK_KINDS = {ErrorKind.RATE_LIMIT, ErrorKind.OVERLOADED, ErrorKind.SERVER, ErrorKind.TIMEOUT, ErrorKind.NETWORK, ErrorKind.NOT_FOUND}


class ResilientProvider(Provider):
    """包装一个或多个 (provider, model)：先在主模型上按策略重试，失败后沿降级链切换。"""

    def __init__(self, chain: list[tuple[Provider, str]], policy: RetryPolicy | None = None,
                 breaker_threshold: int = 5, breaker_reset: float = 30.0):
        if not chain:
            raise ValueError("降级链不能为空")
        self.chain = chain
        self.policy = policy or RetryPolicy()
        self.breakers = {i: CircuitBreaker(breaker_threshold, breaker_reset) for i in range(len(chain))}
        self.name = chain[0][0].name

    def model_info(self, model: str) -> ModelInfo:
        return self.chain[0][0].model_info(model)

    async def aclose(self) -> None:
        seen = set()
        for p, _ in self.chain:
            if id(p) not in seen:
                seen.add(id(p))
                await p.aclose()

    async def stream(self, req: ModelRequest, cancel: CancelToken) -> AsyncIterator[StreamEvent]:
        last: ProviderError | None = None
        for i, (provider, model) in enumerate(self.chain):
            breaker = self.breakers[i]
            if not breaker.allow():
                last = ProviderError(ErrorKind.OVERLOADED, f"{provider.name}/{model} 熔断中", provider=provider.name)
                continue
            if i > 0:
                yield StreamEvent("notice", info={"code": "fallback", "text": f"降级到 {provider.name}/{model}"})
            sub = req if i == 0 else replace(req, model=model)
            attempt = 0
            while True:
                emitted = False
                try:
                    async for ev in provider.stream(sub, cancel):
                        if ev.type != "start":
                            emitted = True
                        yield ev
                    breaker.success()
                    return
                except ProviderError as e:
                    last = e
                    if e.retryable:
                        breaker.failure()
                    else:
                        breaker.release()  # 鉴权失败、请求非法等不代表服务不健康
                    attempt += 1
                    if not e.retryable or attempt >= self.policy.max_attempts or not breaker.allow():
                        break
                    delay = self.policy.backoff(attempt, e.retry_after)
                    left = self.policy.time_left()
                    if left is not None and left < delay:
                        break
                    yield StreamEvent("retry", info={
                        "attempt": attempt, "delay": round(delay, 2), "error": str(e),
                        "kind": e.kind, "discard_partial": emitted, "model": model,
                    })
                    await cancel.sleep(delay)
                except BaseException:
                    breaker.release()  # 取消、生成器被关闭等：结果不说明服务健康与否，归还探测名额
                    raise
            if last is not None and last.kind not in _FALLBACK_KINDS:
                raise last
        assert last is not None
        raise last
