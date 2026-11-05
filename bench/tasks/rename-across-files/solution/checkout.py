"""结账小票。"""

from pricing import compute_total


def receipt(order):
    return f"{order['id']}: {compute_total(order['items'])}"
