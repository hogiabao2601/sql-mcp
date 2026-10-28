-- Deterministic seed data (no RAND), so results are reproducible. Dates are relative to
-- the moment the container is first started, so "orders yesterday" always returns rows.
USE shop;

INSERT INTO customers (name, email, city, created_at) VALUES
  ('Ann Nguyen',   'ann@example.test',   'Ho Chi Minh City', NOW() - INTERVAL 200 DAY),
  ('Bob Tran',     'bob@example.test',   'Hanoi',            NOW() - INTERVAL 180 DAY),
  ('Cara Le',      'cara@example.test',  'Da Nang',          NOW() - INTERVAL 160 DAY),
  ('Dan Pham',     'dan@example.test',   'Ho Chi Minh City', NOW() - INTERVAL 140 DAY),
  ('Eva Vo',       'eva@example.test',   'Hue',              NOW() - INTERVAL 120 DAY),
  ('Finn Hoang',   'finn@example.test',  'Hanoi',            NOW() - INTERVAL 100 DAY),
  ('Gia Bui',      'gia@example.test',   'Can Tho',          NOW() - INTERVAL 80 DAY),
  ('Hugo Dang',    'hugo@example.test',  'Da Nang',          NOW() - INTERVAL 60 DAY),
  ('Ivy Ly',       'ivy@example.test',   'Ho Chi Minh City', NOW() - INTERVAL 40 DAY),
  ('Jack Truong',  'jack@example.test',  'Hanoi',            NOW() - INTERVAL 20 DAY);

INSERT INTO products (name, category, price) VALUES
  ('Notebook A5',        'stationery',  3.50),
  ('Gel Pen Set',        'stationery',  5.25),
  ('Desk Lamp',          'home',       24.90),
  ('Water Bottle',       'home',       12.00),
  ('USB-C Cable',        'electronics', 8.99),
  ('Wireless Mouse',     'electronics',19.99),
  ('Backpack',           'bags',       42.00),
  ('Coffee Mug',         'home',        9.50);

-- 40 orders spread over the last 7 days. The recursive CTE just counts 1..40.
INSERT INTO orders (customer_id, status, total, created_at)
WITH RECURSIVE n (i) AS (
  SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 40
)
SELECT 1 + ((i * 7) % 10),
       ELT(1 + (i % 5), 'paid', 'paid', 'shipped', 'shipped', 'cancelled'),
       0,
       NOW() - INTERVAL (i % 7) DAY - INTERVAL ((i * 37) % 1000) MINUTE
FROM n;

-- Up to 3 distinct products per order (offsets 3, 6, 9 are distinct mod 8).
INSERT INTO order_items (order_id, product_id, quantity, unit_price)
SELECT o.id, p.id, 1 + ((o.id + k.k) % 3), p.price
FROM orders o
JOIN (SELECT 1 AS k UNION ALL SELECT 2 UNION ALL SELECT 3) k
  ON (o.id + k.k) % 4 <> 0
JOIN products p
  ON p.id = 1 + ((o.id + k.k * 3) % 8);

-- Fill in each order's total from its items.
UPDATE orders o
JOIN (SELECT order_id, SUM(quantity * unit_price) AS t FROM order_items GROUP BY order_id) x
  ON x.order_id = o.id
SET o.total = x.t;
