"""报表的列。列名来自配置文件，所以这里记的是函数名，运行时再查找。"""

import pricing

COLUMNS = {
    "subtotal": "calc_subtotal",
    "tax": "calc_total_tax",
    "total": "compute_total",
}


def column_value(column, items):
    return getattr(pricing, COLUMNS[column])(items)
