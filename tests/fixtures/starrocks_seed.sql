-- StarRocks port of mysql-init/ for the conformance suite (the all-in-one image has no init directory, so the
-- suite loads this file once). Same formulas: 40 orders, 90 items, totals 2279.25. TEST-ONLY credentials.
-- Single backend: every table is PRIMARY KEY, one bucket, replication_num 1. StarRocks has no foreign keys.
DROP DATABASE IF EXISTS shop FORCE;
DROP DATABASE IF EXISTS analytics FORCE;
DROP DATABASE IF EXISTS `odd-names` FORCE;
CREATE DATABASE shop;
CREATE DATABASE analytics;
CREATE DATABASE `odd-names`;

CREATE TABLE shop.customers (id INT NOT NULL, name VARCHAR(60) NOT NULL, email VARCHAR(120) NOT NULL,
  city VARCHAR(60) NOT NULL, created_at DATETIME NOT NULL)
  PRIMARY KEY (id) DISTRIBUTED BY HASH (id) BUCKETS 1 PROPERTIES ("replication_num" = "1");
CREATE TABLE shop.products (id INT NOT NULL, name VARCHAR(80) NOT NULL, category VARCHAR(40) NOT NULL,
  price DECIMAL(10,2) NOT NULL)
  PRIMARY KEY (id) DISTRIBUTED BY HASH (id) BUCKETS 1 PROPERTIES ("replication_num" = "1");
CREATE TABLE shop.orders (id INT NOT NULL, customer_id INT NOT NULL, status VARCHAR(10) NOT NULL,
  total DECIMAL(10,2) NOT NULL, created_at DATETIME NOT NULL)
  PRIMARY KEY (id) DISTRIBUTED BY HASH (id) BUCKETS 1 PROPERTIES ("replication_num" = "1");
CREATE TABLE shop.order_items (id INT NOT NULL, order_id INT NOT NULL, product_id INT NOT NULL, quantity INT NOT NULL,
  unit_price DECIMAL(10,2) NOT NULL)
  PRIMARY KEY (id) DISTRIBUTED BY HASH (id) BUCKETS 1 PROPERTIES ("replication_num" = "1");

INSERT INTO shop.customers VALUES
  (1, 'Ann Nguyen', 'ann@example.test', 'Ho Chi Minh City', days_sub(now(), 200)),
  (2, 'Bob Tran', 'bob@example.test', 'Hanoi', days_sub(now(), 180)),
  (3, 'Cara Le', 'cara@example.test', 'Da Nang', days_sub(now(), 160)),
  (4, 'Dan Pham', 'dan@example.test', 'Ho Chi Minh City', days_sub(now(), 140)),
  (5, 'Eva Vo', 'eva@example.test', 'Hue', days_sub(now(), 120)),
  (6, 'Finn Hoang', 'finn@example.test', 'Hanoi', days_sub(now(), 100)),
  (7, 'Gia Bui', 'gia@example.test', 'Can Tho', days_sub(now(), 80)),
  (8, 'Hugo Dang', 'hugo@example.test', 'Da Nang', days_sub(now(), 60)),
  (9, 'Ivy Ly', 'ivy@example.test', 'Ho Chi Minh City', days_sub(now(), 40)),
  (10, 'Jack Truong', 'jack@example.test', 'Hanoi', days_sub(now(), 20));
INSERT INTO shop.products VALUES
  (1, 'Notebook A5', 'stationery', 3.50), (2, 'Gel Pen Set', 'stationery', 5.25), (3, 'Desk Lamp', 'home', 24.90),
  (4, 'Water Bottle', 'home', 12.00), (5, 'USB-C Cable', 'electronics', 8.99), (6, 'Wireless Mouse', 'electronics', 19.99),
  (7, 'Backpack', 'bags', 42.00), (8, 'Coffee Mug', 'home', 9.50);
INSERT INTO shop.order_items
SELECT row_number() OVER (ORDER BY o.i, k.k), o.i, p.id, 1 + ((o.i + k.k) % 3), p.price
FROM (SELECT generate_series AS i FROM TABLE(generate_series(1, 40))) o
JOIN (SELECT 1 AS k UNION ALL SELECT 2 UNION ALL SELECT 3) k ON (o.i + k.k) % 4 <> 0
JOIN shop.products p ON p.id = 1 + ((o.i + k.k * 3) % 8);
INSERT INTO shop.orders
SELECT n.i, 1 + ((n.i * 7) % 10),
       CASE n.i % 5 WHEN 0 THEN 'paid' WHEN 1 THEN 'paid' WHEN 2 THEN 'shipped' WHEN 3 THEN 'shipped' ELSE 'cancelled' END,
       COALESCE(t.total, 0), minutes_sub(days_sub(now(), n.i % 7), (n.i * 37) % 1000)
FROM (SELECT generate_series AS i FROM TABLE(generate_series(1, 40))) n
LEFT JOIN (SELECT order_id, SUM(quantity * unit_price) AS total FROM shop.order_items GROUP BY order_id) t
  ON t.order_id = n.i;

CREATE TABLE analytics.app_users (id INT NOT NULL, email VARCHAR(120) NOT NULL, password_hash VARCHAR(60) NOT NULL
  COMMENT 'bcrypt hash', api_token VARCHAR(32) NOT NULL, created_at DATETIME NOT NULL)
  PRIMARY KEY (id) DISTRIBUTED BY HASH (id) BUCKETS 1 PROPERTIES ("replication_num" = "1");
CREATE TABLE analytics.page_views (id BIGINT NOT NULL, user_id INT NULL, path VARCHAR(200) NOT NULL,
  viewed_at DATETIME NOT NULL)
  PRIMARY KEY (id) DISTRIBUTED BY HASH (id) BUCKETS 1 PROPERTIES ("replication_num" = "1");
INSERT INTO analytics.app_users VALUES
  (1, 'ann@example.test', '$2b$12$aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'tok_ann_0123456789abcdef012345', days_sub(now(), 30)),
  (2, 'bob@example.test', '$2b$12$bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'tok_bob_0123456789abcdef012345', days_sub(now(), 20)),
  (3, 'cara@example.test', '$2b$12$ccccccccccccccccccccccccccccccccccccccccccccccccccccc', 'tok_cara_123456789abcdef012345', days_sub(now(), 10));
INSERT INTO analytics.page_views
SELECT i, CASE WHEN i % 5 = 0 THEN NULL ELSE 1 + (i % 3) END,
       CASE i % 4 WHEN 0 THEN '/home' WHEN 1 THEN '/pricing' WHEN 2 THEN '/docs' ELSE '/checkout' END,
       minutes_sub(days_sub(now(), i % 5), (i * 53) % 1300)
FROM (SELECT generate_series AS i FROM TABLE(generate_series(1, 120))) n;

CREATE TABLE `odd-names`.`order-archive` (id INT NOT NULL, note VARCHAR(40) NOT NULL)
  PRIMARY KEY (id) DISTRIBUTED BY HASH (id) BUCKETS 1 PROPERTIES ("replication_num" = "1");
CREATE TABLE `odd-names`.`Order History` (id INT NOT NULL, happened_at DATETIME NOT NULL)
  PRIMARY KEY (id) DISTRIBUTED BY HASH (id) BUCKETS 1 PROPERTIES ("replication_num" = "1");
CREATE TABLE `odd-names`.`café` (id INT NOT NULL, `crème` VARCHAR(20) NOT NULL)
  PRIMARY KEY (id) DISTRIBUTED BY HASH (id) BUCKETS 1 PROPERTIES ("replication_num" = "1");
INSERT INTO `odd-names`.`order-archive` VALUES (1, 'archived'), (2, 'archived too');
INSERT INTO `odd-names`.`Order History` VALUES (1, now());
INSERT INTO `odd-names`.`café` VALUES (1, 'brûlée');

-- readonly_user: SELECT only (the account to register). app_writer: may write shop (StarRocks has no read-only
-- session, so for StarRocks this account shows what the server can NOT stop by itself).
DROP USER IF EXISTS readonly_user;
DROP USER IF EXISTS app_writer;
CREATE USER readonly_user IDENTIFIED BY 'readonly-pw-test-only';
GRANT SELECT ON ALL TABLES IN DATABASE shop TO USER readonly_user;
GRANT SELECT ON ALL TABLES IN DATABASE analytics TO USER readonly_user;
GRANT SELECT ON ALL TABLES IN DATABASE `odd-names` TO USER readonly_user;
CREATE USER app_writer IDENTIFIED BY 'writer-pw-test-only';
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN DATABASE shop TO USER app_writer;
