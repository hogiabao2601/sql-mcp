"""Offline tests for the layer-1 guard and helpers. No database needed.

    .venv/bin/python -m unittest discover -s tests -v

Reminder: the guard is a convenience for the model, NOT a security boundary. The
database-level layers (read-only session, SELECT-only user) are tested by tests/smoke_mysql.py.
"""
import importlib.util
import pathlib
import unittest

from mcp.server.mcpserver.exceptions import ToolError

_spec = importlib.util.spec_from_file_location("ms", pathlib.Path(__file__).parent.parent / "mysql_server.py")
ms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ms)


class AllowedQueries(unittest.TestCase):
    CASES = [
        "SELECT 1",
        "  select * from orders;  ",
        "SHOW TABLES",
        "SHOW CREATE TABLE orders",                      # contains CREATE, but SHOW is exempt from the keyword scan
        "DESCRIBE orders",
        "EXPLAIN SELECT * FROM orders",
        "WITH x AS (SELECT 1 AS a) SELECT * FROM x",
        "SELECT 'a;b' AS semicolon_in_string",           # ; inside a literal is fine (v1 rejected this)
        "SELECT '#tag' AS hash_in_string",
        "SELECT * FROM t WHERE note LIKE '%into update delete%'",
        "SELECT update_time, create_time FROM information_schema.tables",   # \b stops matching at '_'
        "SELECT REPLACE(name, 'a', 'b') FROM t",         # REPLACE() the function is fine
        "SELECT `delete` FROM t",                        # quoted identifier is blanked before scanning
    ]

    def test_allowed(self):
        for sql in self.CASES:
            with self.subTest(sql=sql):
                ms.check_read_only(sql)

    def test_trailing_semicolon_and_whitespace_removed(self):
        self.assertEqual(ms.check_read_only("  SELECT 1 ;  "), "SELECT 1")


class BlockedQueries(unittest.TestCase):
    CASES = [
        ("empty", ""), ("blank", "   "),
        ("drop", "DROP TABLE orders"), ("delete", "DELETE FROM orders"),
        ("update", "UPDATE t SET a=1"), ("insert", "INSERT INTO t VALUES (1)"),
        ("set", "SET GLOBAL read_only=0"), ("call", "CALL do_things()"),
        ("load data", "LOAD DATA INFILE 'x' INTO TABLE t"),
        ("stacked", "SELECT 1; DELETE FROM t"),
        ("stacked no space", "SELECT 1;DELETE FROM t"),
        ("executable comment", "SELECT 1 /*!INTO OUTFILE '/tmp/x'*/"),
        ("dash dash no space", "SELECT 1--1 INTO OUTFILE '/tmp/x'"),
        ("hash comment", "SELECT 1 # hello"),
        ("outfile", "SELECT * INTO OUTFILE '/tmp/x' FROM t"),
        ("outfile via comment", "SELECT 1 INTO/**/OUTFILE '/tmp/x'"),
        ("dumpfile", "SELECT 1 INTO DUMPFILE '/tmp/x'"),
        ("sleep", "SELECT SLEEP(5)"), ("benchmark", "SELECT BENCHMARK(1000000, MD5('a'))"),
        ("get_lock", "SELECT GET_LOCK('a', 1)"), ("load_file", "SELECT LOAD_FILE('/etc/passwd')"),
        ("explain analyze", "EXPLAIN ANALYZE SELECT 1"),
        ("for update", "SELECT * FROM t FOR UPDATE"),
        ("cte delete", "WITH x AS (SELECT 1) DELETE FROM t"),
    ]

    def test_blocked(self):
        for label, sql in self.CASES:
            with self.subTest(label=label):
                with self.assertRaises(ToolError):
                    ms.check_read_only(sql)


class Helpers(unittest.TestCase):
    def test_cell_binary_and_long_text(self):
        self.assertEqual(ms._cell(b"\x00\x01"), "<binary 2 bytes>")
        long = "x" * (ms.MAX_CELL + 25)
        self.assertTrue(ms._cell(long).endswith("...(+25 chars)"))
        self.assertEqual(ms._cell(7), 7)
        self.assertIsNone(ms._cell(None))

    def test_identifier_validation(self):
        for bad in ["a`b", "x; DROP", "", "a b", "a" * 65]:
            with self.subTest(bad=bad), self.assertRaises(ToolError):
                ms.check_ident(bad, "table")
        self.assertEqual(ms.check_ident("order_items", "table"), "order_items")


if __name__ == "__main__":
    unittest.main()
