import itertools
import sys

sys.path.insert(0, sys.argv[1])
from lru import LRUCache

# 穷举短操作序列，和一个按列表维护顺序的参照实现逐步对比
OPS = [(op, key) for op in ("get", "put") for key in "abc"]
for capacity in (1, 2, 3):
    for seq in itertools.product(OPS, repeat=5):
        cache = LRUCache(capacity)
        order, values = [], {}   # order 从最久没用到最近使用
        for step, (op, key) in enumerate(seq):
            if op == "get":
                got = cache.get(key, "miss")
                want = values[key] if key in order else "miss"
                assert got == want, f"capacity={capacity} 序列 {seq[:step + 1]}：get({key!r}) 得到 {got!r}，应为 {want!r}"
                if key in order:
                    order.remove(key)
                    order.append(key)
            else:
                cache.put(key, step)
                if key in order:
                    order.remove(key)
                elif len(order) == capacity:
                    order.pop(0)
                order.append(key)
                values[key] = step
            kept = [k for k in "abc" if k in cache]
            assert kept == sorted(order), f"capacity={capacity} 序列 {seq[:step + 1]}：缓存里是 {kept}，应为 {sorted(order)}"
            assert len(cache) == len(order), f"capacity={capacity} 序列 {seq[:step + 1]}：len 不对"

# 存的值是 None 时也是命中，现有的 get 就是这样区分命中和没命中的
cache = LRUCache(2)
cache.put("a", None)
cache.put("b", 2)
assert cache.get("a", "miss") is None, "值为 None 的键也算命中"
cache.put("c", 3)
assert "a" in cache and "b" not in cache, "值为 None 的键被 get 之后也算最近使用"
