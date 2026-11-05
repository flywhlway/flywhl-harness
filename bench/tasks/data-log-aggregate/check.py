"""比对 status.csv。路径和响应大小里都混着 404、500，请求行是 "-" 的记录字段数也不同，按位置或第一个三位数取都会错。"""

import csv
import sys
from pathlib import Path

EXPECTED = [["status", "count"], ["200", "88"], ["304", "17"], ["404", "12"], ["500", "12"], ["301", "7"], ["403", "5"],
            ["503", "3"], ["408", "2"], ["400", "1"]]

path = Path(sys.argv[1], "status.csv")
if not path.is_file():
    sys.exit("缺 status.csv")
with path.open(newline="", encoding="utf-8") as f:
    rows = [row for row in csv.reader(f) if row]
assert rows[:1] == EXPECTED[:1], f"表头不对：{rows[:1]}"
assert sorted(rows[1:]) == sorted(EXPECTED[1:]), f"统计结果不对：{rows[1:]}"
assert rows == EXPECTED, f"排序不对：{rows[1:]}"
