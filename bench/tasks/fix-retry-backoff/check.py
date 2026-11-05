import sys

sys.path.insert(0, sys.argv[1])
from retry import retry


class BadRequest(ValueError):
    pass


def run(results, attempts, base_delay):
    """fn 按顺序取 results，异常就抛，别的就返回；取完后重复最后一个。"""
    calls = []
    sleeps = []

    def fn():
        value = results[min(len(calls), len(results) - 1)]
        calls.append(value)
        if isinstance(value, Exception):
            raise value
        return value

    try:
        outcome = retry(fn, attempts, base_delay, sleep=sleeps.append)
    except Exception as e:
        outcome = e
    return outcome, len(calls), sleeps


def fails(n):
    return [ConnectionError(f"第 {i} 次失败") for i in range(1, n + 1)]


first, second, third = OSError("磁盘"), KeyError("键"), TimeoutError("超时")
denied, bad_request = ValueError("参数不对"), BadRequest("子类也是 ValueError")
three, five, one = fails(3), fails(5), fails(1)

CASES = [
    # 结果序列, attempts, base_delay, 期望结局, 期望调用次数, 期望的等待
    (three, 3, 0.5, three[-1], 3, [0.5, 1.0]),
    (five, 5, 3, five[-1], 5, [3, 6, 12, 24]),
    (one, 1, 2, one[0], 1, []),
    ([first, second, third], 3, 1, third, 3, [1, 2]),
    (["ok"], 4, 1, "ok", 1, []),
    (fails(2) + ["ok"], 5, 2, "ok", 3, [2, 4]),
    (fails(1) + [0], 2, 1, 0, 2, [1]),
    ([denied], 5, 1, denied, 1, []),
    ([first, denied, "ok"], 5, 1, denied, 2, [1]),
    ([bad_request, "ok"], 5, 1, bad_request, 1, []),
]
for results, attempts, base_delay, want, want_calls, want_sleeps in CASES:
    label = f"attempts={attempts} base_delay={base_delay} 结果序列={results!r}"
    outcome, called, sleeps = run(results, attempts, base_delay)
    assert called == want_calls, f"{label}：调用了 {called} 次，应为 {want_calls} 次"
    assert sleeps == want_sleeps, f"{label}：等待序列是 {sleeps}，应为 {want_sleeps}"
    if isinstance(want, Exception):
        assert outcome is want, f"{label}：结局是 {outcome!r}，应当抛出 {want!r}"
    else:
        assert outcome == want and not isinstance(outcome, Exception), f"{label}：结局是 {outcome!r}，应当返回 {want!r}"
