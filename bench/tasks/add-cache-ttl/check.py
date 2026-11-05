import sys

sys.path.insert(0, sys.argv[1])
from cache import Cache


class Clock:
    def __init__(self, now=0):
        self.now = now

    def __call__(self):
        return self.now


# 过期边界，时钟从 0 开始
clock = Clock()
cache = Cache(ttl=10, clock=clock)
cache.set("a", 1)
clock.now = 9.5
assert cache.get("a") == 1, "还没到 ttl 就过期了"
clock.now = 10
assert cache.get("a", "过期") == "过期", "正好 ttl 秒时已经过期"
clock.now = 11
assert cache.get("a") is None, "过期后 default 默认是 None"

# 再次 set 重新计时，get 不续期
clock = Clock(100)
cache = Cache(ttl=5, clock=clock)
cache.set("a", 1)
clock.now = 104
cache.set("a", 2)
clock.now = 108
assert cache.get("a") == 2, "再次 set 要重新计时"
clock.now = 109
assert cache.get("a", "过期") == "过期", "get 不应续期"

# 假值也是缓存的值
clock = Clock()
cache = Cache(ttl=5, clock=clock)
for key, value in (("none", None), ("zero", 0), ("empty", "")):
    cache.set(key, value)
    assert cache.get(key, "默认") == value, f"存进去的 {value!r} 被当成没命中"
clock.now = 5
assert cache.get("none", "默认") == "默认", "值为 None 的条目也要过期"

# ttl=0：写入时就已经满 ttl 秒
cache = Cache(ttl=0, clock=Clock(7))
cache.set("a", 1)
assert cache.get("a", "过期") == "过期", "ttl=0 不等于不过期"

# 不过期
clock = Clock()
for cache in (Cache(ttl=None, clock=clock), Cache(clock=clock)):
    cache.set("a", 1)
    clock.now += 1e9
    assert cache.get("a") == 1, "ttl 为 None 时不过期"
    assert cache.purge() == 0, "ttl 为 None 时没有可清理的条目"
    assert len(cache) == 1, "ttl 为 None 时 purge 不该删东西"

# 默认参数：原来的写法继续工作，默认时钟下刚写入的键不会过期
cache = Cache()
cache.set("a", 1)
assert cache.get("a") == 1, "Cache() 的原有用法坏了"
cache = Cache(ttl=1000)
cache.set("a", 1)
assert cache.get("a") == 1, "默认时钟下刚写入的键不该过期"

# purge
clock = Clock()
cache = Cache(ttl=10, clock=clock)
cache.set("old1", 1)
cache.set("old2", 2)
clock.now = 6
cache.set("new", 3)
clock.now = 10
assert cache.purge() == 2, "purge 应当返回删除的条数"
assert len(cache) == 1, "purge 之后只剩没过期的条目"
assert cache.get("new") == 3, "purge 不能删没过期的条目"
assert cache.purge() == 0, "第二次 purge 没有东西可删"
clock.now = 16
assert cache.purge() == 1, "new 到 16 秒时过期"
assert len(cache) == 0, "全部过期后 purge 应当删空"

# 过期功能和 delete 共存
clock = Clock()
cache = Cache(ttl=10, clock=clock)
cache.set("a", 1)
cache.delete("a")
cache.delete("没有这个键")
assert cache.get("a", "没了") == "没了" and len(cache) == 0, "delete 坏了"
