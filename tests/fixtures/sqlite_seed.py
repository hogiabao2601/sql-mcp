"""Build the SQLite test databases: the same logical data as mysql-init/ and docker/pg-init/.

    .venv/bin/python tests/fixtures/sqlite_seed.py [target_dir]      # default: .smoke/sqlite

Writes shop.db, analytics.db, odd-names.db and wide.db (400 tables, for the schema:// cap) as READ-ONLY
files (0444): that is how sql-mcp should be deployed for SQLite, because SQLite has no accounts (layer 3
is the file's permissions). shop-writable.db (0644) exists only to prove that layer 2 refuses writes even
when the file itself is writable. Dates are relative to "now" (UTC), like the other seeds.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

SHOP = """
PRAGMA foreign_keys = ON;
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL, city TEXT NOT NULL,
                        created_at TEXT NOT NULL);
CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, price NUMERIC NOT NULL);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
                     status TEXT NOT NULL CHECK (status IN ('paid', 'shipped', 'cancelled')),
                     total NUMERIC NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE order_items (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
                          product_id INTEGER NOT NULL REFERENCES products(id), quantity INTEGER NOT NULL,
                          unit_price NUMERIC NOT NULL);
CREATE INDEX order_items_order_id ON order_items (order_id);
INSERT INTO customers (name, email, city, created_at) VALUES
  ('Ann Nguyen', 'ann@example.test', 'Ho Chi Minh City', datetime('now', '-200 days')),
  ('Bob Tran', 'bob@example.test', 'Hanoi', datetime('now', '-180 days')),
  ('Cara Le', 'cara@example.test', 'Da Nang', datetime('now', '-160 days')),
  ('Dan Pham', 'dan@example.test', 'Ho Chi Minh City', datetime('now', '-140 days')),
  ('Eva Vo', 'eva@example.test', 'Hue', datetime('now', '-120 days')),
  ('Finn Hoang', 'finn@example.test', 'Hanoi', datetime('now', '-100 days')),
  ('Gia Bui', 'gia@example.test', 'Can Tho', datetime('now', '-80 days')),
  ('Hugo Dang', 'hugo@example.test', 'Da Nang', datetime('now', '-60 days')),
  ('Ivy Ly', 'ivy@example.test', 'Ho Chi Minh City', datetime('now', '-40 days')),
  ('Jack Truong', 'jack@example.test', 'Hanoi', datetime('now', '-20 days'));
INSERT INTO products (name, category, price) VALUES
  ('Notebook A5', 'stationery', 3.50), ('Gel Pen Set', 'stationery', 5.25), ('Desk Lamp', 'home', 24.90),
  ('Water Bottle', 'home', 12.00), ('USB-C Cable', 'electronics', 8.99), ('Wireless Mouse', 'electronics', 19.99),
  ('Backpack', 'bags', 42.00), ('Coffee Mug', 'home', 9.50);
WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 40)
INSERT INTO orders (id, customer_id, status, total, created_at)
SELECT i, 1 + ((i * 7) % 10),
       CASE i % 5 WHEN 0 THEN 'paid' WHEN 1 THEN 'paid' WHEN 2 THEN 'shipped' WHEN 3 THEN 'shipped' ELSE 'cancelled' END,
       0, datetime('now', '-' || (i % 7) || ' days', '-' || ((i * 37) % 1000) || ' minutes')
FROM n;
INSERT INTO order_items (order_id, product_id, quantity, unit_price)
SELECT o.id, p.id, 1 + ((o.id + k.k) % 3), p.price
FROM orders o JOIN (SELECT 1 AS k UNION ALL SELECT 2 UNION ALL SELECT 3) k ON (o.id + k.k) % 4 <> 0
JOIN products p ON p.id = 1 + ((o.id + k.k * 3) % 8)
ORDER BY o.id, k.k;
UPDATE orders SET total = (SELECT ROUND(SUM(quantity * unit_price), 2) FROM order_items WHERE order_id = orders.id);
"""

ANALYTICS = """
CREATE TABLE app_users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
                        api_token TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE page_views (id INTEGER PRIMARY KEY, user_id INTEGER NULL REFERENCES app_users(id), path TEXT NOT NULL,
                         viewed_at TEXT NOT NULL);
CREATE INDEX idx_page_views_viewed_at ON page_views (viewed_at);
INSERT INTO app_users (email, password_hash, api_token, created_at) VALUES
  ('ann@example.test', '$2b$12$aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'tok_ann_0123456789abcdef012345', datetime('now', '-30 days')),
  ('bob@example.test', '$2b$12$bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'tok_bob_0123456789abcdef012345', datetime('now', '-20 days')),
  ('cara@example.test', '$2b$12$ccccccccccccccccccccccccccccccccccccccccccccccccccccc', 'tok_cara_123456789abcdef012345', datetime('now', '-10 days'));
WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 120)
INSERT INTO page_views (user_id, path, viewed_at)
SELECT CASE WHEN i % 5 = 0 THEN NULL ELSE 1 + (i % 3) END,
       CASE i % 4 WHEN 0 THEN '/home' WHEN 1 THEN '/pricing' WHEN 2 THEN '/docs' ELSE '/checkout' END,
       datetime('now', '-' || (i % 5) || ' days', '-' || ((i * 53) % 1300) || ' minutes')
FROM n;
"""

ODD = """
CREATE TABLE "order-archive" (id INTEGER PRIMARY KEY, note TEXT NOT NULL);
CREATE TABLE "Order History" (id INTEGER PRIMARY KEY, happened_at TEXT NOT NULL);
CREATE TABLE "café" (id INTEGER PRIMARY KEY, "crème" TEXT NOT NULL);
INSERT INTO "order-archive" VALUES (1, 'archived'), (2, 'archived too');
INSERT INTO "Order History" VALUES (1, datetime('now'));
INSERT INTO "café" VALUES (1, 'brûlée');
"""

WIDE = "\n".join(f"CREATE TABLE t{i:03d} ({', '.join(f'column_{j:02d} VARCHAR(255)' for j in range(20))});"
                 for i in range(400))


def build(target: pathlib.Path) -> pathlib.Path:
    target.mkdir(parents=True, exist_ok=True)
    for name, script in [("shop", SHOP), ("analytics", ANALYTICS), ("odd-names", ODD), ("wide", WIDE)]:
        path = target / f"{name}.db"
        if path.exists():
            os.chmod(path, 0o644)
            path.unlink()
        con = sqlite3.connect(path)
        con.executescript(script)
        con.commit()
        con.close()
        os.chmod(path, 0o444)
    writable = target / "shop-writable.db"
    if writable.exists():
        os.chmod(writable, 0o644)
        writable.unlink()
    shutil.copyfile(target / "shop.db", writable)
    os.chmod(writable, 0o644)
    return target


if __name__ == "__main__":
    out = build(pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".smoke" / "sqlite")
    print(f"wrote {sorted(p.name for p in out.glob('*.db'))} in {out}")
