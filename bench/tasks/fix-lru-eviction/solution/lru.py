"""进程内的 LRU 缓存。"""

from collections import OrderedDict


class LRUCache:
    """固定容量的缓存，容量满时淘汰最久没用的键。"""

    def __init__(self, capacity):
        if capacity < 1:
            raise ValueError("capacity 至少是 1")
        self.capacity = capacity
        self._items = OrderedDict()

    def get(self, key, default=None):
        if key not in self._items:
            return default
        self._items.move_to_end(key)
        return self._items[key]

    def put(self, key, value):
        if key in self._items:
            self._items.move_to_end(key)
        elif len(self._items) >= self.capacity:
            self._items.popitem(last=False)
        self._items[key] = value

    def __contains__(self, key):
        return key in self._items

    def __len__(self):
        return len(self._items)
