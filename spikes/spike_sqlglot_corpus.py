"""Do ordinary, legitimate MySQL / PostgreSQL reads survive the sqlglot classifier? (false-reject rate)"""
import sys; sys.path.insert(0, "spikes")
from spike_sqlglot import classify
MYSQL = [
 "SELECT status, COUNT(*) OVER (PARTITION BY status) AS n FROM orders",
 "SELECT GROUP_CONCAT(name ORDER BY name SEPARATOR ', ') FROM customers",
 "SELECT * FROM orders WHERE created_at >= NOW() - INTERVAL 7 DAY",
 "SELECT STRAIGHT_JOIN o.id FROM orders o JOIN customers c ON c.id = o.customer_id",
 "SELECT id FROM orders USE INDEX (PRIMARY) WHERE id > 3",
 "SELECT id FROM orders FORCE INDEX (PRIMARY) WHERE id > 3",
 "SELECT * FROM products WHERE MATCH(name) AGAINST ('lamp' IN NATURAL LANGUAGE MODE)",
 "SELECT name FROM customers WHERE name REGEXP '^A'",
 "SELECT CONVERT(name USING utf8mb4) FROM customers",
 "SELECT _utf8mb4'x' COLLATE utf8mb4_bin",
 "SELECT X'4142', b'101', 0x41",
 "SELECT @@version, @@session.time_zone",
 "SELECT doc->>'$.a' FROM t",
 "SELECT JSON_EXTRACT(doc, '$.a') FROM t",
 "SELECT * FROM JSON_TABLE('[1,2]', '$[*]' COLUMNS (x INT PATH '$')) AS jt",
 "SELECT o.id, l.n FROM orders o, LATERAL (SELECT COUNT(*) AS n FROM order_items i WHERE i.order_id = o.id) l",
 "WITH RECURSIVE n (i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 5) SELECT i FROM n",
 "SELECT CAST(total AS SIGNED), CAST(total AS DECIMAL(10,2)) FROM orders",
 "SELECT DATE_FORMAT(created_at, '%Y-%m') AS m, SUM(total) FROM orders GROUP BY m WITH ROLLUP",
 "SELECT * FROM orders PARTITION (p0)",
 "TABLE orders",
 "VALUES ROW(1, 2), ROW(3, 4)",
 "SELECT IF(total > 10, 'big', 'small'), IFNULL(NULL, 1), COALESCE(NULL, 2) FROM orders",
 "SELECT SQL_NO_CACHE id FROM orders",
 "SELECT DISTINCTROW status FROM orders",
 "SELECT * FROM orders ORDER BY id LIMIT 5 OFFSET 10",
 "SELECT * FROM orders LIMIT 10, 5",
 "SELECT id FROM orders WHERE status IN ('paid', 'shipped') AND total BETWEEN 1 AND 100",
 "SELECT 1 # hash comment",
 "SELECT /*+ MAX_EXECUTION_TIME(100000) */ 1",
 "SELECT /*+ SET_VAR(sort_buffer_size = 16M) */ 1",
 "SELECT a.* FROM `shop`.`order-archive` a",
]
PG = [
 "SELECT status, count(*) FILTER (WHERE total > 10) FROM shop.orders GROUP BY status",
 "SELECT * FROM shop.orders WHERE created_at >= now() - interval '7 days'",
 "SELECT id::text, total::numeric(10,2) FROM shop.orders",
 "SELECT DISTINCT ON (customer_id) customer_id, created_at FROM shop.orders ORDER BY customer_id, created_at DESC",
 "SELECT * FROM shop.orders o, LATERAL (SELECT count(*) FROM shop.order_items i WHERE i.order_id = o.id) l",
 "SELECT jsonb_build_object('a', 1) -> 'a', '{\"a\":1}'::jsonb ->> 'a'",
 "SELECT array_agg(id ORDER BY id), string_agg(status, ',') FROM shop.orders",
 "SELECT * FROM shop.orders WHERE status ~* '^p' AND status ILIKE 'p%'",
 "SELECT * FROM unnest(ARRAY[1,2,3]) WITH ORDINALITY AS t(x, n)",
 "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY total) FROM shop.orders",
 "SELECT * FROM shop.orders FETCH FIRST 3 ROWS ONLY",
 "SELECT * FROM shop.orders TABLESAMPLE SYSTEM (10)",
 "SELECT 1 /* block */ -- line",
 "SELECT current_setting('statement_timeout')",
]
for d, qs in [("mysql", MYSQL), ("postgres", PG)]:
    bad = [(q, r) for q in qs if (r := classify(q, d)) != "ok"]
    print(f"{d}: {len(qs) - len(bad)}/{len(qs)} legitimate reads accepted")
    for q, r in bad:
        print(f"   REJECTED  {r[:70]:<70} | {q}")
