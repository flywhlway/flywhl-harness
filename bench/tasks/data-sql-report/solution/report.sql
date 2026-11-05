SELECT c.name AS name, SUM(o.amount_cents) AS total
FROM customers AS c
JOIN orders AS o ON o.customer_id = c.id
LEFT JOIN refunds AS r ON r.order_id = o.id
WHERE o.status = 'paid' AND r.order_id IS NULL
GROUP BY c.id
ORDER BY total DESC, c.name ASC
LIMIT 3;
