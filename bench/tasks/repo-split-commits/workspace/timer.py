"""秒表。"""

import time


class Stopwatch:
    def __init__(self):
        self.started_at = time.monotonic()

    def elapsed_ms(self):
        return round((time.monotonic() - self.started_at) * 1000)
