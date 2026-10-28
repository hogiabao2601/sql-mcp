"""External-catalog conformance for StarRocks (Iceberg, Hive). Proves sql-mcp reads an external catalog
read-only AND that its safety nets still hold there: masking resolves external columns via DESC, the guard
refuses writes and FILES(), and a SELECT-only account cannot INSERT.

    SQL_MCP_SR_CATALOG=iceberg_cat .venv/bin/python -m unittest tests.conformance.starrocks_external -v
    SQL_MCP_SR_CATALOG=hive_cat    ...

Expects a database `lake` with a table `events` whose columns include a secret-looking `api_token`
(built by tests/fixtures/make_external_catalogs.py). Ground truth comes from a separate root connection.
"""
from __future__ import annotations

import json
import os
import unittest

import pymysql

from mcp.server.mcpserver.exceptions import ToolError

from sql_mcp import server as srv
from sql_mcp.config import config
from sql_mcp.executor import close_pools, run
from sql_mcp.config import get_profile

HOST, PORT = "127.0.0.1", int(os.environ.get("SQL_MCP_STARROCKS_PORT") or 39030)
CATALOG = os.environ.get("SQL_MCP_SR_CATALOG", "iceberg_cat")
DB, TABLE, SECRET = "lake", "events", "api_token"


def admin(sql):
    c = pymysql.connect(host=HOST, port=PORT, user="root", password="", autocommit=True)
    try:
        with c.cursor() as k:
            k.execute(sql)
            return k.fetchall() if k.description else None
    finally:
        c.close()


def write_config():
    import pathlib
    path = pathlib.Path(__file__).resolve().parents[2] / ".smoke" / f"ext-{CATALOG}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"profiles": {"lake": {
        "engine": "starrocks", "host": HOST, "port": PORT, "user": "readonly_user",
        "password": "readonly-pw-test-only", "catalog": CATALOG, "database": DB,
        "mask_columns": ["token", "password"]}}}))
    return str(path)


os.environ["SQL_MCP_CONFIG"] = write_config()
os.environ["SQL_MCP_CACHE_TTL"] = "0"


def j(text):
    return json.loads(text)


class External(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            present = admin("SHOW CATALOGS")
            names = {r[0] for r in present}
            if CATALOG not in names:
                raise unittest.SkipTest(f"catalog {CATALOG} not built; run: .venv/bin/python "
                                        f"tests/fixtures/make_external_catalogs.py {CATALOG.split('_')[0]}")
            if not admin(f"SHOW TABLES FROM {CATALOG}.{DB}"):
                raise unittest.SkipTest(f"{CATALOG}.{DB}.{TABLE} not built (see make_external_catalogs.py)")
        except unittest.SkipTest:
            raise
        except Exception as e:
            raise unittest.SkipTest(f"StarRocks ext stack not up ({str(e).splitlines()[0][:80]})") from None

    def setUp(self):
        config.cache_clear()
        close_pools()

    def test_introspection(self):
        self.assertIn(DB, j(srv.list_databases()))
        self.assertIn(TABLE, [r[0] for r in j(srv.list_tables())["rows"]])
        d = j(srv.describe_table(table=TABLE))
        self.assertTrue(any(c.startswith(SECRET) for c in d["columns"]), d)
        self.assertEqual(d["references"], [])            # external catalogs have no foreign keys
        self.assertIn(f"{DB}.{TABLE}.{SECRET}", [c.split()[0] for c in j(srv.search_schema(keyword="token"))["columns"]])
        self.assertIn(f"{TABLE}(", srv.schema_overview("lake", DB))

    def test_query_matches_ground_truth(self):
        truth = admin(f"SELECT COUNT(*) FROM {CATALOG}.{DB}.{TABLE}")[0][0]
        self.assertEqual(j(srv.execute_query(query=f"SELECT COUNT(*) FROM {TABLE}"))["rows"][0][0], truth)

    def test_masking_resolves_external_columns(self):
        # The gap this guards: external columns are NOT in information_schema, so masking must DESC them.
        for q in [f"SELECT {SECRET} FROM {TABLE}", f"SELECT {SECRET} AS x FROM {TABLE}",
                  f"SELECT CONCAT({SECRET}, '') AS x FROM {TABLE}"]:
            with self.subTest(q=q):
                vals = [r[0] for r in j(srv.execute_query(query=q))["rows"]]
                self.assertTrue(vals and all(v == "***" for v in vals), vals)
        with self.assertRaisesRegex(ToolError, "masked column"):
            srv.execute_query(query=f"SELECT id FROM {TABLE} WHERE {SECRET} LIKE 'tok_%'")

    def test_guard_refuses_writes_and_files(self):
        for q in [f"INSERT INTO {TABLE} VALUES (9, 'x', 'y', now())",
                  "SELECT * FROM FILES('path' = 's3://x/', 'format' = 'parquet', 'aws.s3.access_key' = 'k')",
                  f"SELECT * FROM {TABLE} INTO OUTFILE 's3://x/' FORMAT AS CSV"]:
            with self.subTest(q=q), self.assertRaises(ToolError):
                srv.execute_query(query=q)

    def test_select_only_account_cannot_insert(self):
        # Layer 3: a PLAIN connection as the account (no sql-mcp) must be refused by StarRocks itself.
        with self.assertRaisesRegex(Exception, "denied|privilege"):
            run(get_profile("lake"), f"INSERT INTO {TABLE} VALUES (9, 'x', 'y', now())")


def setUpModule():
    import sys
    print(f"\n[external] catalog={CATALOG} db={DB} table={TABLE}", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
