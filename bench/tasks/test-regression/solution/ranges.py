"""区间的合并与统计。区间是 (start, end)，end 不小于 start。"""


def merge_ranges(ranges):
    """合并重叠或相接的区间，返回按 start 升序的新列表。"""
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def total_covered(ranges):
    """返回所有区间覆盖的总长度，重复覆盖的部分只算一次。"""
    return sum(end - start for start, end in merge_ranges(ranges))
