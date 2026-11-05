"""统计日志里每个级别的条数：python3 stats.py app.log"""

import sys
from collections import Counter

from logparse import parse_line


def count_levels(path):
    counts = Counter()
    with open(path, encoding="utf-8") as f:
        for line in f:
            counts[parse_line(line).level] += 1
    return counts


def main():
    counts = count_levels(sys.argv[1])
    for level in sorted(counts):
        print(f"{level}: {counts[level]}")


if __name__ == "__main__":
    main()
