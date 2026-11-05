"""调用外部服务时用的重试工具。"""

import time


def retry(fn, attempts, base_delay, sleep=time.sleep):
    """调用 fn，失败了等一会儿再试，返回 fn 的结果。

    attempts 是总调用次数。等待时间从 base_delay 开始每次翻倍。
    次数用完时抛最后一次的异常；ValueError 不重试，立刻抛出。
    """
    for attempt in range(attempts):
        try:
            return fn()
        except ValueError:
            raise
        except Exception:
            if attempt == attempts - 1:
                raise
            sleep(base_delay * 2**attempt)
