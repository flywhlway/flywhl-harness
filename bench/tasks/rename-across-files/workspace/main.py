"""打印示例订单的小票和报表。"""

from checkout import receipt
from columns import COLUMNS, column_value

ORDERS = [
    {"id": "A-1001", "items": [{"price": 1999, "qty": 2}, {"price": 350, "qty": 1}]},
    {"id": "A-1002", "items": [{"price": 12000, "qty": 1}]},
]


def main():
    for order in ORDERS:
        print(receipt(order))
        for column in COLUMNS:
            print(f"  {column}: {column_value(column, order['items'])}")


if __name__ == "__main__":
    main()
