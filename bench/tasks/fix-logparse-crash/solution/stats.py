"""统计日志里每个级别的条数：python3 stats.py app.log"""

import sys
from collections import Counter

from logparse import parse_line


def count_levels(path):
    counts = Counter()
    bad = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            entry = parse_line(line)
            if entry is None:
                bad += 1
            else:
                counts[entry.level] += 1
    return counts, bad


def main():
    counts, bad = count_levels(sys.argv[1])
    for level in sorted(counts):
        print(f"{level}: {counts[level]}")
    print(f"bad lines: {bad}")


if __name__ == "__main__":
    main()
