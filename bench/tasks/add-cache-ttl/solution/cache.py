"""进程内的小缓存。"""

import time


class Cache:
    def __init__(self, ttl=None, clock=time.monotonic):
        self._ttl = ttl
        self._clock = clock
        self._data = {}  # 键 -> (值, 写入时间)

    def _expired(self, written_at):
        return self._ttl is not None and self._clock() - written_at >= self._ttl

    def set(self, key, value):
        self._data[key] = (value, self._clock())

    def get(self, key, default=None):
        entry = self._data.get(key)
        if entry is None or self._expired(entry[1]):
            return default
        return entry[0]

    def delete(self, key):
        self._data.pop(key, None)

    def purge(self):
        """删除所有已过期的条目，返回删除的条数。"""
        expired = [key for key, (_, written_at) in self._data.items() if self._expired(written_at)]
        for key in expired:
            del self._data[key]
        return len(expired)

    def __len__(self):
        return len(self._data)
