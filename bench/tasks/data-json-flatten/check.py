"""逐行比对 orders.csv。覆盖 null 和缺字段两种缺失、空 coupon 对象、无商品的订单、整数价格、带逗号的姓名。"""

import csv
import sys
from pathlib import Path

HEADER = ["order_id", "customer", "city", "coupon", "sku", "qty", "unit_price", "line_total"]
EXPECTED = [
    ["A-1001", "林晓", "杭州", "SPRING", "PEN-01", "3", "4.50", "13.50"],
    ["A-1001", "林晓", "杭州", "SPRING", "NB-A5", "1", "12.00", "12.00"],
    ["A-1002", "周宁", "", "", "INK-B", "2", "19.99", "39.98"],
    ["A-1003", "Ana, María", "", "", "MUG-3", "4", "7.25", "29.00"],
    ["A-1004", "陈默", "成都", "VIP", "", "", "", ""],
    ["A-1005", "何川", "上海", "", "PEN-01", "10", "0.99", "9.90"],
    ["A-1005", "何川", "上海", "", "CASE-L", "1", "129.90", "129.90"],
    ["A-1005", "何川", "上海", "", "BAG-2", "2", "58.00", "116.00"],
    ["A-1006", "苏禾", "", "", "INK-B", "1", "19.99", "19.99"],
    ["A-1007", "林晓", "杭州", "", "", "", "", ""],
]

path = Path(sys.argv[1], "orders.csv")
if not path.is_file():
    sys.exit("缺 orders.csv")
with path.open(newline="", encoding="utf-8") as f:
    rows = [row for row in csv.reader(f) if row]
assert rows[:1] == [HEADER], f"表头不对：{rows[:1]}"
assert len(rows) - 1 == len(EXPECTED), f"应当有 {len(EXPECTED)} 行数据，实际 {len(rows) - 1} 行"
for got, want in zip(rows[1:], EXPECTED):
    assert got == want, f"这一行不对：{got}，应当是 {want}"
