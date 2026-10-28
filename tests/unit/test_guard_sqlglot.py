"""Offline tests for the sqlglot-based guard (layer 1) and LIMIT injection. No database needed.
The 37 legacy cases stay in tests/test_guard.py and must keep passing unchanged."""
import unittest

from mcp.server.mcpserver.exceptions import ToolError

from sql_mcp.guard import check, check_read_only, with_limit

ALLOWED = {
    "mysql": [
        # review #11: the regex guard refused these legitimate reads
        "SELECT load FROM servers", "SELECT c.amount AS `call` FROM t c", "SELECT h.* FROM handler h",
        "SELECT 1--1", "(SELECT 1) UNION (SELECT 2)", "SELECT * FROM t WHERE kill_switch = 0",
        # everyday MySQL
        "SELECT status, COUNT(*) OVER (PARTITION BY status) AS n FROM orders",
        "SELECT GROUP_CONCAT(name ORDER BY name SEPARATOR ', ') FROM customers",
        "SELECT * FROM orders WHERE created_at >= NOW() - INTERVAL 7 DAY",
        "SELECT id FROM orders USE INDEX (PRIMARY) WHERE id > 3",
        "SELECT * FROM JSON_TABLE('[1,2]', '$[*]' COLUMNS (x INT PATH '$')) AS jt",
        "WITH RECURSIVE n (i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 5) SELECT i FROM n",
        "SELECT DATE_FORMAT(created_at, '%Y-%m') AS m, SUM(total) FROM orders GROUP BY m WITH ROLLUP",
        "SELECT * FROM orders LIMIT 10, 5", "SELECT a.* FROM `shop`.`order-archive` a", "SELECT X'4142', b'101', 0x41",
        # parity with the regex guard: SHOW / DESCRIBE / EXPLAIN stay available
        "SHOW FULL TABLES", "SHOW INDEX FROM orders", "SHOW TABLE STATUS", "SHOW VARIABLES LIKE 'max%'",
        "SHOW COLUMNS FROM orders WHERE Field = 'id'", "DESC orders", "EXPLAIN FORMAT=TREE SELECT * FROM orders",
    ],
    "postgres": [
        "SELECT \"order-archive\".id FROM shop.\"order-archive\"", "SELECT $$it's$$ AS dollar_quoted",
        "SELECT 'a\\' AS standard_string", "SELECT E'a\\'b' AS escape_string",
        "SELECT status, count(*) FILTER (WHERE total > 10) FROM shop.orders GROUP BY status",
        "SELECT DISTINCT ON (customer_id) customer_id FROM shop.orders ORDER BY customer_id, created_at DESC",
        "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY total) FROM shop.orders",
        "SELECT * FROM unnest(ARRAY[1,2,3]) WITH ORDINALITY AS t(x, n)", "SELECT nextval('s')",
    ],
    "sqlite": ["SELECT * FROM \"order-archive\"",
               "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT x FROM c LIMIT 5"],
    "starrocks": ["SELECT * FROM shop.orders LIMIT 5", "SELECT * FROM TABLE(generate_series(1, 3))",
                  "SELECT * FROM files", "SELECT files FROM t",           # a table / column named files is fine
                  "SELECT * FROM default_catalog.shop.orders"],
}

REFUSED = {   # (sql, a fragment of the expected message)
    "mysql": [
        ("SELECT `sleep`(3)", "sleep"), ("SELECT SOURCE_POS_WAIT('f', 4, 10)", "source_pos_wait"),
        ("DESC ANALYZE SELECT 1", "ANALYZE"), ("DESCRIBE DELETE FROM t", "DESCRIBE"),
        ("SELECT id FROM t FOR SHARE", "lock"), ("SELECT id FROM t LOCK IN SHARE MODE", "lock"),
        ("SHOW DATABASES WHERE (SELECT COUNT(*) FROM a, b) > 0", "subquery"),
        ("SELECT 'a\\', 1 INTO OUTFILE '/tmp/x'", "parse"),
        ("SELECT /*+ MAX_EXECUTION_TIME(60000) */ 1", "hint"), ("SELECT /*+ SET_VAR(sort_buffer_size = 16M) */ 1", "hint"),
        ("SELECT 1 /* x */", "comment"), ("SELECT 1 -- x", "comment"), ("TABLE orders", "SELECT"),
        ("SELECT 1; SELECT 2", "one statement"), ("CALL p()", "SELECT"), ("HANDLER t OPEN", "SELECT"),
    ],
    "postgres": [
        ("WITH d AS (DELETE FROM shop.orders RETURNING *) SELECT * FROM d", "Delete"),
        ("SELECT set_config('default_transaction_read_only', 'off', false)", "set_config"),
        ("SELECT pg_sleep(10)", "pg_sleep"), ("SELECT * FROM shop.orders FOR UPDATE", "lock"),
        ("SELECT * FROM shop.orders FOR KEY SHARE", "lock"), ("COPY shop.orders TO '/tmp/x'", "SELECT"),
        ("SELECT lo_import('/etc/passwd')", "lo_import"), ("SELECT * INTO new_t FROM shop.orders", "INTO"),
        ("SET default_transaction_read_only = off", "SELECT"), ("BEGIN READ WRITE", "SELECT"),
        ("DO $$BEGIN DELETE FROM t; END$$", "SELECT"), ("SHOW statement_timeout", "SELECT"),
    ],
    "sqlite": [("ATTACH DATABASE '/tmp/x.db' AS x", "SELECT"), ("PRAGMA query_only = OFF", "SELECT"),
               ("SELECT load_extension('x')", "load_extension"), ("VACUUM INTO '/tmp/copy.db'", "SELECT")],
    "starrocks": [("SELECT /*+ SET_VAR(query_timeout = 60) */ 1", "hint"), ("SELECT SLEEP(3)", "sleep"),
                  ("SET query_timeout = 3600", "SELECT"), ("CACHE SELECT * FROM shop.orders", "SELECT"),
                  ("SELECT * FROM shop.orders INTO OUTFILE 'file:///tmp/x/' FORMAT AS CSV", "parse"),
                  ("INSERT INTO FILES('path' = 's3://x/', 'format' = 'csv') SELECT 1", "parse"),
                  ("SUBMIT TASK AS INSERT INTO t SELECT 1", "SELECT"),
                  # FILES() reads (and with INSERT writes) any storage, with keys given in the statement itself
                  ("SELECT * FROM FILES('path' = 's3://x/', 'format' = 'parquet', 'aws.s3.access_key' = 'k')", "files"),
                  ("SELECT o.id FROM orders o JOIN FILES('path' = 's3://x/', 'format' = 'csv') f ON f.id = o.id", "files"),
                  ("WITH x AS (SELECT * FROM FILES('path' = 'file:///etc/', 'format' = 'csv')) SELECT * FROM x", "files")],
}


class GuardTests(unittest.TestCase):
    def test_allowed(self):
        for dialect, cases in ALLOWED.items():
            for sql in cases:
                with self.subTest(dialect=dialect, sql=sql):
                    check(sql, dialect)

    def test_refused_with_a_reason(self):
        for dialect, cases in REFUSED.items():
            for sql, fragment in cases:
                with self.subTest(dialect=dialect, sql=sql):
                    with self.assertRaises(ToolError) as cm:
                        check(sql, dialect)
                    self.assertIn(fragment.lower(), str(cm.exception).lower())
                    self.assertGreater(len(str(cm.exception)), 15)

    def test_mariadb_parses_as_mysql(self):
        check("SELECT JSON_VALUE(doc, '$.a') FROM t", "mariadb")
        with self.assertRaises(ToolError):
            check("SELECT SLEEP(1)", "mariadb")

    def test_legacy_api_returns_cleaned_text(self):
        self.assertEqual(check_read_only("  SELECT 1 ;  "), "SELECT 1")


class LimitInjection(unittest.TestCase):
    """with_limit returns (sql, capped). It only ever APPENDS to the caller's text, never regenerates it:
    a regenerated FETCH FIRST query once came back as LIMIT 101 and returned 38 rows instead of 3."""
    def cap(self, sql, n=101, dialect="mysql"):
        return with_limit(check(sql, dialect), n)

    def test_appends_to_the_original_text(self):
        self.assertEqual(self.cap("select  id from t order by id"), ("select  id from t order by id LIMIT 101", True))
        self.assertEqual(self.cap("SELECT 1 UNION SELECT 2"), ("SELECT 1 UNION SELECT 2 LIMIT 101", True))
        self.assertEqual(self.cap("WITH x AS (SELECT 1 AS a) SELECT a FROM x"),
                         ("WITH x AS (SELECT 1 AS a) SELECT a FROM x LIMIT 101", True))
        self.assertEqual(self.cap("SELECT m, SUM(x) FROM t GROUP BY m WITH ROLLUP"),
                         ("SELECT m, SUM(x) FROM t GROUP BY m WITH ROLLUP LIMIT 101", True))

    def test_keeps_a_smaller_limit(self):
        self.assertEqual(self.cap("SELECT id FROM t LIMIT 5"), ("SELECT id FROM t LIMIT 5", True))
        self.assertEqual(self.cap("SELECT id FROM t LIMIT 10, 5"), ("SELECT id FROM t LIMIT 10, 5", True))

    def test_never_rewrites_a_larger_limit_or_fetch_or_offset(self):
        for sql, dialect in [("SELECT id FROM t LIMIT 10, 5000", "mysql"),
                             ("SELECT id FROM t ORDER BY id OFFSET 2 ROWS FETCH FIRST 3 ROWS ONLY", "mariadb"),
                             ("SELECT id FROM t ORDER BY id FETCH FIRST 3 ROWS ONLY", "postgres"),
                             ("SELECT id FROM t ORDER BY id OFFSET 2", "postgres"),
                             ("SELECT id FROM t LIMIT ALL", "postgres")]:
            with self.subTest(sql=sql):
                self.assertEqual(self.cap(sql, dialect=dialect), (sql, False))

    def test_show_and_describe_are_not_rewritten(self):
        self.assertEqual(self.cap("SHOW TABLES"), ("SHOW TABLES", False))
        self.assertEqual(self.cap("DESCRIBE orders"), ("DESCRIBE orders", False))

if __name__ == "__main__":
    unittest.main()
