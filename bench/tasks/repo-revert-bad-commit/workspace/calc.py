"""结算小工具。"""


def subtotal(items):
    """items 是 (单价, 数量) 的列表。"""
    return sum(price * qty for price, qty in items)


def apply_discount(amount, percent):
    """按百分比打折，结果保留两位小数。"""
    return round(amount * (100 - percent) / 100, 2)


def split_bill(amount, people):
    """平摊账单，返回每人应付，保留两位小数。"""
    if people < 1:
        raise ValueError("people 至少是 1")
    return round(amount / people, 2)
