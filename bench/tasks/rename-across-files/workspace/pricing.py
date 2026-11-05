"""订单金额计算，金额都以分为单位。"""

TAX_PERCENT = 6


def calc_subtotal(items):
    return sum(item["price"] * item["qty"] for item in items)


def calc_total_tax(items):
    return calc_subtotal(items) * TAX_PERCENT // 100


def calc_total(items):
    return calc_subtotal(items) + calc_total_tax(items)
