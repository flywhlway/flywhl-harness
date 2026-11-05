"""结账小票。"""

from pricing import calc_total


def receipt(order):
    return f"{order['id']}: {calc_total(order['items'])}"
