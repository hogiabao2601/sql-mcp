"""Live regression tests for REVIEW.md findings, against the Docker test MySQL.

    docker compose up -d --wait
    .venv/bin/python -m unittest discover -s tests -p 'live_*.py' -v
    SQL_MCP_TARGET=/path/to/module.py ...     # run the same tests against another implementation

The target module must expose run(), get_profile(), config (with cache_clear) and _pools (a dict).
"""
import importlib.util
import json
import os
import pathlib
import sys
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from smoke_mysql import resolve_config, root_sql  # noqa: E402

from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

PORT = json.loads(pathlib.Path(resolve_config()).read_text())["profiles"]["shop"]["port"]
BASE = {"host": "127.0.0.1", "port": PORT, "user": "readonly_user", "password_env": "TEST_RO_PASSWORD"}
CFG = ROOT / ".smoke" / "live-profiles.json"
CFG.parent.mkdir(exist_ok=True)
CFG.write_text(json.dumps({"default": "shop", "profiles": {
    "shop": dict(BASE, database="shop", pool_size=3),
    "nodb": dict(BASE, pool_size=1),
    "one": dict(BASE, database="shop", pool_size=1),
    "analytics": dict(BASE, database="analytics", mask_columns=["password", "token", "email"]),
    **{f"tls_{m}": dict(BASE, database="shop", tls=m, **({"tls_ca": str(ROOT / ".smoke" / "ca.pem")} if m.startswith("verify") else {}))
       for m in ("disable", "prefer", "require", "verify-ca", "verify-full")},
    "tls_wrong_ca": dict(BASE, database="shop", tls="verify-ca", tls_ca=str(ROOT / ".smoke" / "wrong-ca.pem")),
}}))
os.environ.update(MYSQL_CONFIG=str(CFG), TEST_RO_PASSWORD="readonly-pw-test-only", MYSQL_CACHE_TTL="0")
TARGET = pathlib.Path(os.environ.get("SQL_MCP_TARGET", ROOT / "mysql_server.py"))
_spec = importlib.util.spec_from_file_location("target", TARGET)
ms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ms)


def ro_connections() -> int:
    return int(root_sql("SELECT COALESCE(SUM(total_connections),0) FROM performance_schema.accounts WHERE user='readonly_user'"))


def init_db_count() -> int:
    return int(root_sql("SELECT COUNT_STAR FROM performance_schema.events_statements_summary_global_by_event_name "
                        "WHERE EVENT_NAME='statement/com/Init DB'"))


def running_ro_queries() -> list[str]:
    out = root_sql("SELECT id FROM information_schema.processlist WHERE user='readonly_user' AND command='Query'")
    return out.split()


class Regressions(unittest.TestCase):
    def setUp(self):
        ms.config.cache_clear()
        ms._pools.clear()

    def test_r1_waiter_gets_a_connection_when_the_busy_one_is_discarded(self):
        p = ms.get_profile("one")
        res = {}

        def holder():   # truncated -> today's code discards the connection
            ms.run(p, "SELECT o.id FROM (SELECT SLEEP(1) AS s) x, shop.orders o", max_rows=1)

        def waiter():
            t0 = time.monotonic()
            try:
                ms.run(p, "SELECT 1")
                res["ok"] = time.monotonic() - t0
            except ToolError as e:
                res["err"] = f"{e} after {time.monotonic() - t0:.1f}s"
        a = threading.Thread(target=holder)
        a.start()
        time.sleep(0.2)
        b = threading.Thread(target=waiter)
        b.start()
        a.join()
        b.join()
        self.assertIn("ok", res, res)
        self.assertLess(res["ok"], 3)

    def test_r2_no_database_means_no_database_not_the_previous_calls(self):
        p = ms.get_profile("nodb")
        self.assertEqual(ms.run(p, "SELECT DATABASE()", database="analytics")["rows"], [["analytics"]])
        self.assertEqual(ms.run(p, "SELECT DATABASE()")["rows"], [[None]])

    def test_r18_same_database_sends_no_init_db(self):
        p = ms.get_profile("one")
        ms.run(p, "SELECT 1")
        before = init_db_count()
        for _ in range(20):
            ms.run(p, "SELECT 1")
        self.assertEqual(init_db_count() - before, 0)

    def test_r6_failed_statements_keep_their_connection(self):
        p = ms.get_profile("shop")
        ms.run(p, "SELECT 1")
        c0 = ro_connections()
        for _ in range(10):
            with self.assertRaises(ToolError):
                ms.run(p, "SELECT no_such_column FROM orders")
        self.assertEqual(ro_connections() - c0, 0)

    def test_server_side_timeout_leaves_a_reusable_connection(self):
        p = ms.get_profile("one")
        ms.run(p, "SELECT 1")
        c0 = ro_connections()
        with self.assertRaisesRegex(ToolError, "5000 ms limit"):
            ms.run(p, "SELECT COUNT(*) FROM order_items a, order_items b, order_items c, order_items d, order_items e "
                      "WHERE (a.id + b.id + c.id + d.id + e.id) % 7 = 0")
        self.assertEqual(ms.run(p, "SELECT 1")["rows"], [[1]])
        self.assertEqual(ro_connections() - c0, 0)

    def test_r6_truncated_queries_keep_their_connection(self):
        ms.execute_query(query="SELECT 1")
        c0 = ro_connections()
        for _ in range(20):
            out = json.loads(ms.execute_query(query="SELECT a.id FROM order_items a, order_items b", max_rows=10))
            self.assertEqual((out["row_count"], out.get("truncated")), (10, "row limit 10"))
        self.assertEqual(ro_connections() - c0, 0)

    def test_r3_show_with_subquery_is_refused_before_reaching_the_server(self):
        with self.assertRaisesRegex(ToolError, "subquery"):
            ms.execute_query(query="SHOW DATABASES WHERE (SELECT COUNT(*) FROM order_items a, order_items b) > 0")

    def test_r24_optimizer_hint_cannot_raise_the_time_limit(self):
        with self.assertRaisesRegex(ToolError, "hint"):
            ms.execute_query(query="SELECT /*+ MAX_EXECUTION_TIME(60000) */ COUNT(*) FROM order_items")

    def test_r8_session_sql_mode_agrees_with_the_guard(self):
        old = root_sql("SELECT @@GLOBAL.sql_mode")
        try:
            root_sql(f"SET GLOBAL sql_mode = 'NO_BACKSLASH_ESCAPES,ANSI_QUOTES,{old}'")
            ms._pools.clear()
            mode = json.loads(ms.execute_query(query="SELECT @@SESSION.sql_mode AS m"))["rows"][0][0]
            self.assertNotIn("NO_BACKSLASH_ESCAPES", mode)
            self.assertNotIn("ANSI_QUOTES", mode)
            self.assertIn("STRICT_TRANS_TABLES", mode)          # everything else is kept
        finally:
            root_sql(f"SET GLOBAL sql_mode = '{old}'")
            ms._pools.clear()

    def test_r4_masking_follows_the_source_column(self):
        def rows(q):
            return json.loads(ms.execute_query(query=q, profile="analytics"))["rows"]
        for q in ["SELECT api_token AS x FROM app_users",
                  "SELECT CONCAT(api_token, '') AS harmless_name FROM app_users",
                  "SELECT 'n/a' AS x UNION ALL SELECT api_token FROM app_users",
                  "SELECT t.x FROM (SELECT api_token AS x FROM app_users) t"]:
            with self.subTest(q=q):
                values = [r[0] for r in rows(q)]
                self.assertFalse([v for v in values if "tok_" in str(v)], values)

    def test_r4_masked_column_in_where_is_refused(self):
        with self.assertRaisesRegex(ToolError, "masked column 'api_token'"):
            ms.execute_query(query="SELECT id FROM app_users WHERE api_token LIKE 'tok_a%'", profile="analytics")

    def test_r4_error_text_cannot_carry_a_masked_value(self):
        with self.assertRaises(ToolError) as cm:
            ms.execute_query(query="SELECT EXTRACTVALUE(1, CONCAT(0x7e, (SELECT api_token FROM app_users LIMIT 1))) AS x",
                             profile="analytics")
        self.assertNotIn("tok_", str(cm.exception))
        self.assertIn("hidden", str(cm.exception))

    def test_r5_schema_resource_is_complete_or_says_what_is_missing(self):
        text = ms.schema_overview("shop", "information_schema")
        truth = int(root_sql("SELECT COUNT(DISTINCT table_name) FROM information_schema.columns "
                             "WHERE table_schema='information_schema'"))
        lines = text.splitlines()
        missing = int(lines[-1].split()[1]) if lines[-1].startswith("... ") else 0
        shown = len(lines) - (1 if missing else 0)
        self.assertEqual(shown + missing, truth, f"{shown} shown + {missing} reported missing != {truth}")

    def test_r5_internal_catalog_reads_never_truncate_silently(self):
        with self.assertRaisesRegex(ToolError, "too large"):
            ms.rows_of(ms.get_profile("shop"), "SELECT a.id FROM order_items a, order_items b, order_items c")

    def test_r16_discarding_a_streamed_connection_prints_no_traceback(self):
        import gc
        seen = []
        old = sys.unraisablehook
        sys.unraisablehook = lambda u: seen.append(f"{u.exc_type.__name__}: {u.exc_value}")
        try:
            r = ms.run(ms.get_profile("one"), "SELECT a.id FROM order_items a, order_items b", max_rows=5)
            self.assertEqual(r["truncated"], "row limit 5")          # not capped: this path DISCARDS the connection
            gc.collect()
        finally:
            sys.unraisablehook = old
        self.assertEqual(seen, [])

    def test_r3_client_deadline_kills_the_query_and_never_retries(self):
        p = ms.get_profile("one")
        heavy = ("SHOW DATABASES WHERE (SELECT COUNT(*) FROM order_items a, order_items b, order_items c, order_items d, "
                 "order_items e WHERE (a.id + b.id + c.id + d.id + e.id) % 7 = 0) >= 0")
        t0 = time.monotonic()
        try:
            with self.assertRaisesRegex(ToolError, "ms limit"):
                ms.run(p, heavy)
            elapsed = time.monotonic() - t0
            time.sleep(0.5)
            left = running_ro_queries()
            self.assertLess(elapsed, 25, f"took {elapsed:.1f}s: was the statement re-run?")
            self.assertEqual(left, [], "statement still running on the server after the deadline")
        finally:
            for i in running_ro_queries():          # never leave runaway queries behind, pass or fail
                root_sql(f"KILL {i}")


class Identifiers(unittest.TestCase):    # REVIEW #12: legitimate names that the strict regex refused
    def setUp(self):
        ms.config.cache_clear()
        ms._pools.clear()

    def test_awkward_names_work_through_every_tool(self):
        tables = [r[0] for r in json.loads(ms.list_tables(database="odd-names"))["rows"]]
        self.assertEqual(sorted(tables), sorted(["order-archive", "Order History", "café"]))
        for t in tables:
            with self.subTest(table=t):
                d = json.loads(ms.describe_table(table=t, database="odd-names"))
                self.assertTrue(d["columns"][0].startswith("id int"), d)
                self.assertGreaterEqual(json.loads(ms.sample_rows(table=t, database="odd-names"))["row_count"], 1)
        self.assertIn("café(", ms.schema_overview("shop", "odd-names"))
        out = json.loads(ms.execute_query(query="SELECT COUNT(*) FROM `order-archive`", database="odd-names"))
        self.assertEqual(out["rows"], [[2]])

    def test_injection_attempts_are_still_refused_with_the_old_messages(self):
        with self.assertRaisesRegex(ToolError, "Invalid table name"):
            ms.describe_table(table="orders`; DROP TABLE x; --")
        with self.assertRaisesRegex(ToolError, "Invalid table name"):
            ms.sample_rows(table="order-archive`; DROP TABLE x; --", database="odd-names")
        with self.assertRaisesRegex(ToolError, "Invalid database name"):
            ms.describe_table(table="orders", database="shop`; DROP")
        with self.assertRaisesRegex(ToolError, "not found"):
            ms.describe_table(table="nope")


class Tls(unittest.TestCase):          # REVIEW #7: every mode does what it says, against the real server
    @classmethod
    def setUpClass(cls):
        import subprocess
        from smoke_mysql import CONTAINER, DOCKER
        smoke = ROOT / ".smoke"
        subprocess.run([DOCKER, "cp", f"{CONTAINER}:/var/lib/mysql/ca.pem", str(smoke / "ca.pem")], check=True)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=Wrong CA",
                        "-keyout", str(smoke / "wrong-key.pem"), "-out", str(smoke / "wrong-ca.pem")],
                       check=True, capture_output=True)

    def setUp(self):
        ms.config.cache_clear()
        ms._pools.clear()

    def cipher(self, profile):
        rows = ms.run(ms.get_profile(profile), "SHOW SESSION STATUS LIKE 'Ssl_cipher'", mask=False)["rows"]
        return rows[0][1]

    def test_disable_is_plaintext(self):
        self.assertEqual(self.cipher("tls_disable"), "")

    def test_prefer_and_require_encrypt(self):
        self.assertNotEqual(self.cipher("tls_prefer"), "")
        self.assertNotEqual(self.cipher("tls_require"), "")

    def test_verify_ca_accepts_the_right_ca(self):
        self.assertNotEqual(self.cipher("tls_verify-ca"), "")

    def test_verify_ca_refuses_a_wrong_ca(self):
        with self.assertRaisesRegex(ToolError, "CERTIFICATE_VERIFY_FAILED"):
            self.cipher("tls_wrong_ca")

    def test_verify_full_checks_the_host_name(self):
        # MySQL's auto-generated certificate has no SAN for 127.0.0.1, so verify-full must refuse it.
        with self.assertRaisesRegex(ToolError, "CERTIFICATE_VERIFY_FAILED"):
            self.cipher("tls_verify-full")

    def test_list_profiles_reports_the_mode(self):
        profiles = {p["name"]: p for p in json.loads(ms.list_profiles())["profiles"]}
        self.assertEqual((profiles["tls_prefer"]["tls"], profiles["tls_prefer"]["tls_verified"]), (False, False))
        self.assertEqual((profiles["tls_verify-full"]["tls"], profiles["tls_verify-full"]["tls_verified"]), (True, True))


if __name__ == "__main__":
    unittest.main()
