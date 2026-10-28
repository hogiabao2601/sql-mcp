"""Single-file SQLite (`path`) must work: a regression for the ATTACH-as-main bug (the file is already the
connection's implicit `main`, so it must not be re-attached). Offline — SQLite needs no server."""
import json
import os
import pathlib
import sqlite3
import tempfile
import unittest


class SingleFileSQLite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = pathlib.Path(tempfile.mkdtemp(prefix="sqlmcp-1file-"))
        db = cls.dir / "app.db"
        con = sqlite3.connect(db)
        con.executescript("CREATE TABLE orders (id INTEGER PRIMARY KEY, total NUMERIC);"
                           "INSERT INTO orders VALUES (1, 10), (2, 20);")
        con.commit()
        con.close()
        os.chmod(db, 0o444)
        cfg = cls.dir / "c.json"
        cfg.write_text(json.dumps({"profiles": {"app": {"engine": "sqlite", "path": str(db)}}}))
        cls.saved = os.environ.get("SQL_MCP_CONFIG")
        os.environ["SQL_MCP_CONFIG"] = str(cfg)
        os.environ["SQL_MCP_CACHE_TTL"] = "0"

    @classmethod
    def tearDownClass(cls):
        from sql_mcp.config import config
        if cls.saved is None:
            os.environ.pop("SQL_MCP_CONFIG", None)
        else:
            os.environ["SQL_MCP_CONFIG"] = cls.saved
        config.cache_clear()

    def setUp(self):
        from sql_mcp.config import config
        from sql_mcp.executor import close_pools
        config.cache_clear()
        close_pools()

    def test_tools_work_on_a_single_file(self):
        from sql_mcp import server as srv
        self.assertEqual(json.loads(srv.list_databases()), ["main"])
        self.assertEqual([r[0] for r in json.loads(srv.list_tables())["rows"]], ["orders"])
        self.assertEqual(json.loads(srv.execute_query(query="SELECT COUNT(*) FROM orders"))["rows"], [[2]])
        self.assertEqual(json.loads(srv.execute_query(query="SELECT SUM(total) FROM main.orders"))["rows"], [[30]])
        self.assertTrue(json.loads(srv.describe_table(table="orders"))["columns"][0].lower().startswith("id int"))


if __name__ == "__main__":
    unittest.main()
