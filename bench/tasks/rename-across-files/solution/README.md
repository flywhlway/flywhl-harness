# 订单金额

`pricing.py` 提供三个计算函数，金额都以分为单位：

- `calc_subtotal(items)`：商品小计
- `calc_total_tax(items)`：小计对应的税额
- `compute_total(items)`：应付总额，等于小计加税额

报表有哪些列在 `columns.py` 里配置。运行示例：`python3 main.py`
