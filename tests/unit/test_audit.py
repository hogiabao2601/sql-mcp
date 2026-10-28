"""Offline tests for the audit log (REVIEW #9). Uses the SQLite engine, so no database server is needed."""
import json
import os
import pathlib
import stat
import unittest

from mcp.server.mcpserver.exceptions import ToolError

from sql_mcp import audit
from sql_mcp.guard import check

ROOT = pathlib.Path(__file__).resolve().parents[2]
LOG = ROOT / ".smoke" / "audit-test.jsonl"


class Fingerprint(unittest.TestCase):
    def test_literals_become_placeholders(self):
        fp = audit.fingerprint(check("SELECT * FROM t WHERE email = 'ann@example.test' AND id IN (1, 2) LIMIT 5"))
        self.assertNotIn("ann@example.test", fp)
        self.assertNotIn("1, 2", fp)
        self.assertIn("email = ?", fp)


class AuditLog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.fixtures.sqlite_seed import build
        d = ROOT / ".smoke" / "sqlite"
        if not (d / "shop.db").exists():
            build(d)
        cfg = ROOT / ".smoke" / "audit-profiles.json"
        cfg.write_text(json.dumps({"profiles": {"shop": {"engine": "sqlite", "database": "shop", "timeout_ms": 500, "databases": {
            "shop": str(d / "shop.db"), "analytics": str(d / "analytics.db")}}}}))
        os.environ["SQL_MCP_CONFIG"] = str(cfg)

    def setUp(self):
        from sql_mcp.config import config
        config.cache_clear()
        if LOG.exists():
            LOG.unlink()
        os.environ["SQL_MCP_AUDIT_LOG"] = str(LOG)
        os.environ.pop("SQL_MCP_AUDIT_LITERALS", None)
        from sql_mcp.server import build_server, TOOLS
        build_server()                                   # wraps the tools with auditing
        self.tools = {f.__name__: f for f in TOOLS}

    def tearDown(self):
        os.environ.pop("SQL_MCP_AUDIT_LOG", None)

    def lines(self):
        return [json.loads(x) for x in LOG.read_text().splitlines()]

    def call(self, name, **kw):
        from sql_mcp.server import audited_tools
        try:
            return audited_tools()[name](**kw)
        except ToolError as e:
            return e

    def test_one_line_per_call_without_literals(self):
        self.call("execute_query", query="SELECT name FROM customers WHERE email = 'ann@example.test'")
        [e] = self.lines()
        self.assertEqual((e["tool"], e["profile"], e["engine"], e["outcome"], e["rows"]),
                         ("execute_query", "shop", "sqlite", "ok", 1))
        self.assertNotIn("ann@example.test", LOG.read_text())
        self.assertIn("email = ?", e["statement"])
        self.assertEqual(len(e["sha256"]), 16)
        self.assertTrue(e["id"])

    def test_outcomes(self):
        self.call("execute_query", query="DELETE FROM orders")
        self.call("execute_query", query="SELECT no_such_column FROM orders")
        self.call("execute_query", query="WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
                                         "SELECT count(*) FROM c")
        self.assertEqual([e["outcome"] for e in self.lines()], ["refused", "error", "timeout"])
        self.assertIn("DELETE", self.lines()[0]["reason"])
        self.assertNotIn("no such column", LOG.read_text())     # the engine error TEXT (can echo values) is not logged

    def test_literals_only_when_asked(self):
        os.environ["SQL_MCP_AUDIT_LITERALS"] = "1"
        self.call("execute_query", query="SELECT name FROM customers WHERE email = 'ann@example.test'")
        self.assertIn("ann@example.test", self.lines()[0]["sql"])

    def test_file_is_private_and_off_by_default(self):
        self.call("list_profiles")
        self.assertEqual(stat.S_IMODE(LOG.stat().st_mode), 0o600)
        LOG.unlink()
        del os.environ["SQL_MCP_AUDIT_LOG"]
        self.call("list_profiles")
        self.assertFalse(LOG.exists())


if __name__ == "__main__":
    unittest.main()
