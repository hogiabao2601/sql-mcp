-- PostgreSQL port of mysql-init (spike subset). MySQL "databases" shop/analytics become schemas
-- of one database `app`. Same row counts and the same secret-looking columns.
CREATE SCHEMA shop;
CREATE SCHEMA analytics;

CREATE TABLE shop.customers (id int PRIMARY KEY, name varchar(60) NOT NULL, email varchar(120) NOT NULL,
                             city varchar(60) NOT NULL, created_at timestamp NOT NULL);
CREATE TABLE shop.products  (id int PRIMARY KEY, name varchar(80) NOT NULL, category varchar(40) NOT NULL,
                             price numeric(10,2) NOT NULL);
CREATE TABLE shop.orders    (id int PRIMARY KEY, customer_id int NOT NULL REFERENCES shop.customers(id),
                             status varchar(10) NOT NULL, total numeric(10,2) NOT NULL DEFAULT 0,
                             created_at timestamp NOT NULL);
CREATE TABLE shop.order_items (id int PRIMARY KEY, order_id int NOT NULL REFERENCES shop.orders(id),
                               product_id int NOT NULL REFERENCES shop.products(id),
                               quantity int NOT NULL, unit_price numeric(10,2) NOT NULL);
CREATE INDEX ON shop.order_items (order_id);
CREATE TABLE "shop"."order-archive" (id int PRIMARY KEY, note text);          -- legit quoted name (review #12)

INSERT INTO shop.customers SELECT i, 'Customer ' || i, 'c' || i || '@example.test', 'City ' || (i % 4), now() - i * interval '20 day'
  FROM generate_series(1, 10) i;
INSERT INTO shop.products SELECT i, 'Product ' || i, CASE WHEN i % 2 = 0 THEN 'home' ELSE 'stationery' END, 3.5 * i
  FROM generate_series(1, 8) i;
INSERT INTO shop.orders SELECT i, 1 + (i % 10), (ARRAY['paid','shipped','cancelled'])[1 + i % 3], 10 * i, now() - (i % 7) * interval '1 day'
  FROM generate_series(1, 40) i;
INSERT INTO shop.order_items SELECT i, 1 + (i % 40), 1 + (i % 8), 1 + (i % 3), 2.5 * (1 + i % 8)
  FROM generate_series(1, 90) i;

CREATE TABLE analytics.app_users (id int PRIMARY KEY, email varchar(120) NOT NULL UNIQUE, password_hash char(60) NOT NULL,
                                  api_token char(32) NOT NULL, created_at timestamp NOT NULL);
INSERT INTO analytics.app_users VALUES
  (1, 'ann@example.test',  repeat('a', 60), 'tok_ann_0123456789abcdef012345', now()),
  (2, 'bob@example.test',  repeat('b', 60), 'tok_bob_0123456789abcdef012345', now()),
  (3, 'cara@example.test', repeat('c', 60), 'tok_cara_123456789abcdef012345', now());

-- Accounts, as in mysql-init: a SELECT-only user, and a writer used only to prove the read-only session layer.
CREATE ROLE readonly_user LOGIN PASSWORD 'readonly-pw-test-only';
GRANT USAGE ON SCHEMA shop, analytics TO readonly_user;
GRANT SELECT ON ALL TABLES IN SCHEMA shop, analytics TO readonly_user;
CREATE ROLE app_writer LOGIN PASSWORD 'writer-pw-test-only';
GRANT USAGE ON SCHEMA shop TO app_writer;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA shop TO app_writer;
