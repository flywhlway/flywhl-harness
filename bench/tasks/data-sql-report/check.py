"""report.sql 先在 shop.sql 上跑，再换一份内嵌的数据跑同一条查询，防止把结果写死。"""

import sqlite3
import sys
from pathlib import Path

ws = Path(sys.argv[1])

OTHER_MONTH = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
                     amount_cents INTEGER NOT NULL, status TEXT NOT NULL);
CREATE TABLE refunds (order_id INTEGER PRIMARY KEY REFERENCES orders(id), refunded_at TEXT NOT NULL);
INSERT INTO customers (id, name) VALUES (1, 'Ada'), (2, 'Ben'), (3, 'Cid'), (4, 'Dee'), (5, 'Ben'), (6, 'Eve');
INSERT INTO orders (id, customer_id, amount_cents, status) VALUES
    (1, 1, 500, 'paid'), (2, 1, 700, 'paid'), (3, 2, 900, 'paid'), (4, 5, 900, 'paid'),
    (5, 3, 2000, 'pending'), (6, 3, 100, 'paid'), (7, 4, 1200, 'paid'), (8, 4, 400, 'paid');
INSERT INTO refunds (order_id, refunded_at) VALUES (2, '2026-04-01'), (7, '2026-04-02');
"""

report = ws / "report.sql"
if not report.is_file():
    sys.exit("缺 report.sql")
query = report.read_text(encoding="utf-8")


def run(script):
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(script)
        cursor = db.execute(query)
        return [col[0] for col in cursor.description], [tuple(row) for row in cursor.fetchall()]
    finally:
        db.close()


columns, rows = run((ws / "shop.sql").read_text(encoding="utf-8"))
assert columns == ["name", "total"], f"列名不对：{columns}"
assert rows == [("何川", 20000), ("林晓", 20000), ("苏禾", 16000)], f"shop.sql 的结果不对：{rows}"
_, rows = run(OTHER_MONTH)
assert rows == [("Ben", 900), ("Ben", 900), ("Ada", 500)], f"换一份数据后结果不对：{rows}"
