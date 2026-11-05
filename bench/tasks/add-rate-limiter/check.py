import sys

sys.path.insert(0, sys.argv[1])
from limiter import TokenBucket


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def bucket(capacity, rate, start=0):
    clock = Clock(start)
    return TokenBucket(capacity=capacity, rate=rate, clock=clock), clock


# 下面的时间和速率都选二进制能精确表示的小数，比较不受浮点误差影响

# 刚创建是满的，时间不走就不补充
b, clock = bucket(3, 1)
got = [b.allow() for _ in range(4)]
assert got == [True, True, True, False], f"容量 3 的新桶连续 allow 得到 {got}"

# 不够时一个都不扣
b, clock = bucket(5, 1)
assert b.allow(3) is True, "5 个令牌够扣 3 个"
assert b.allow(3) is False, "只剩 2 个，不够扣 3 个"
assert b.allow(2) is True, "失败的 allow 不能扣令牌"
assert b.allow() is False, "令牌应当正好用完"

# n 超过容量时永远不够，也不影响桶里的令牌
b, clock = bucket(3, 1000, start=20)
clock.now = 120
assert b.allow(4) is False, "n 大于 capacity 时不够"
assert b.allow(3) is True, "n 大于 capacity 的失败不能扣令牌"

# 补充的小数部分要累积，失败的 allow 不能把它丢掉
b, clock = bucket(1, 2, start=50)
assert b.allow() is True
for now, want in ((50.25, False), (50.5, True), (50.625, False), (50.75, False), (51, True)):
    clock.now = now
    assert b.allow() is want, f"rate=2、容量 1：{now} 秒时 allow 应为 {want}"

# rate 小于 1
b, clock = bucket(2, 0.5, start=7)
assert b.allow(2) is True
clock.now = 8
assert b.allow() is False, "补了 0.5 个，不够 1 个"
clock.now = 9
assert b.allow() is True, "累计补了 1 个"
clock.now = 11
assert b.allow(2) is False, "现在只有 1 个"
clock.now = 13
assert b.allow(2) is True, "现在有 2 个"

# 令牌不超过 capacity
b, clock = bucket(2, 10)
assert b.allow(2) is True
clock.now = 1000
assert b.allow(2) is True, "闲置很久后桶应当是满的"
assert b.allow() is False, "令牌不能超过 capacity"

# 默认时钟
b = TokenBucket(2, 1)
assert b.allow(2) is True, "默认时钟下新桶也是满的"
