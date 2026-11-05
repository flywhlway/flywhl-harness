"""调用外部服务时用的重试工具。"""

import time


def retry(fn, attempts, base_delay, sleep=time.sleep):
    """调用 fn，失败了等一会儿再试，返回 fn 的结果。"""
    delay = base_delay
    for _ in range(attempts + 1):
        try:
            return fn()
        except Exception:
            sleep(delay)
            delay += base_delay
    return None
