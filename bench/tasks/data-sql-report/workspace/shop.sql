CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    amount_cents INTEGER NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE refunds (
    order_id INTEGER PRIMARY KEY REFERENCES orders(id),
    refunded_at TEXT NOT NULL
);

INSERT INTO customers (id, name) VALUES
    (1, '林晓'), (2, '王芳'), (3, '周宁'), (4, '陈默'), (5, '何川'), (6, '苏禾'), (7, '王芳');

INSERT INTO orders (id, customer_id, amount_cents, status) VALUES
    (1, 1, 12000, 'paid'),
    (2, 1, 8000, 'paid'),
    (3, 2, 15000, 'paid'),
    (4, 7, 14000, 'paid'),
    (5, 3, 30000, 'paid'),
    (6, 3, 9000, 'paid'),
    (7, 4, 50000, 'pending'),
    (8, 4, 6000, 'paid'),
    (9, 5, 18000, 'paid'),
    (10, 5, 2000, 'paid'),
    (11, 6, 40000, 'cancelled'),
    (12, 6, 11000, 'paid'),
    (13, 6, 5000, 'paid'),
    (14, 2, 1000, 'pending'),
    (15, 5, 7000, 'paid');

INSERT INTO refunds (order_id, refunded_at) VALUES
    (5, '2026-03-02'),
    (15, '2026-03-09');
