"""Conformance suite: every engine must pass this before it is supported.

    SQL_MCP_ENGINE=mariadb .venv/bin/python -m unittest tests.conformance.conformance_suite -v
    SQL_MCP_ENGINE=mariadb SQL_MCP_CONFORMANCE_PORT=33327 ...     # another version of the matrix

Needs the engine's Docker test database (see docker/ and docker-compose.yml). Ground truth always
comes from a separate admin connection, never from the server under test. "No reconnect" is proven
by the session id staying the same, which works on every engine (no performance_schema needed).
Everything engine-specific lives in tests/conformance/engines.py; the checks here are the same for all.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import threading
import time
import unittest

from tests.conformance.engines import ENGINE, PORT, SPEC, WRONG_CA, write_config

os.environ["SQL_MCP_CONFIG"] = str(write_config())
os.environ["SQL_MCP_CACHE_TTL"] = "0"

from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from sql_mcp import server as srv  # noqa: E402
from sql_mcp.config import config, get_profile  # noqa: E402
from sql_mcp.executor import close_pools, run  # noqa: E402


def j(text: str):
    return json.loads(text)


def one_value(profile: str, sql: str):
    return run(get_profile(profile), sql, mask=False)["rows"][0][0]


def session_id(profile: str = "one"):
    return one_value(profile, SPEC.session_id)


class Base(unittest.TestCase):
    def setUp(self):
        config.cache_clear()
        close_pools()


class ToolContract(Base):
    def test_list_profiles_never_shows_a_password(self):
        text = srv.list_profiles()
        self.assertEqual(j(text)["default"], "shop")
        self.assertNotIn("readonly-pw", text)
        self.assertNotIn("writer-pw", text)

    def test_list_databases(self):
        dbs = j(srv.list_databases())
        self.assertTrue({"shop", "analytics", "odd-names"} <= set(dbs), dbs)
        self.assertNotIn("information_schema", dbs)
        if SPEC.system_namespace:
            self.assertIn(SPEC.system_namespace, j(srv.list_databases(include_system=True)))

    def test_list_tables(self):
        # Order follows the database's collation: MariaDB sorts "_" after letters, so orders < order_items there.
        names = [r[0] for r in j(srv.list_tables())["rows"]]
        self.assertEqual(sorted(names), ["customers", "order_items", "orders", "products"])
        self.assertEqual(len(names), 4)
        self.assertEqual({r[0] for r in j(srv.list_tables(database="analytics"))["rows"]}, {"app_users", "page_views"})

    def test_describe_table(self):
        d = j(srv.describe_table(table="orders"))
        self.assertTrue(d["columns"][0].lower().startswith("id int"), d["columns"][0])
        if SPEC.has_foreign_keys:
            self.assertIn("customer_id -> shop.customers.id", d["references"])
            self.assertTrue(any(x.startswith("shop.order_items.order_id") for x in d["referenced_by"]), d)
        self.assertTrue(any(x.startswith(SPEC.pk_index) for x in d["indexes"]), d)

    def test_describe_errors(self):
        with self.assertRaisesRegex(ToolError, "not found"):
            srv.describe_table(table="nope")
        with self.assertRaisesRegex(ToolError, "Invalid table name"):
            srv.describe_table(table="orders`; DROP TABLE x; --")
        with self.assertRaisesRegex(ToolError, "Invalid database name"):
            srv.describe_table(table="orders", database="shop`; DROP")

    def test_search_schema(self):
        cols = j(srv.search_schema(keyword="email", database="analytics"))["columns"]
        self.assertTrue(any("analytics.app_users.email" in c for c in cols), cols)
        self.assertTrue({"shop.orders", "shop.order_items"} <= set(j(srv.search_schema(keyword="order"))["tables"]))
        self.assertEqual(j(srv.search_schema(keyword="%")), {"tables": [], "columns": []})

    def test_sample_rows(self):
        r = j(srv.sample_rows(table="app_users", profile="analytics", limit=2))
        row = dict(zip(r["columns"], r["rows"][0], strict=True))
        self.assertEqual((row["password_hash"], row["api_token"], row["email"]), ("***", "***", "***"))
        self.assertEqual(j(srv.sample_rows(table="orders", limit=999))["row_count"], 20)

    def test_answers_match_ground_truth(self):
        truth = SPEC.admin_sql(SPEC.yesterday.replace("FROM orders", "FROM shop.orders"))[0]
        got = j(srv.execute_query(query=SPEC.yesterday))["rows"][0]
        self.assertEqual((str(got[0]), str(got[1])), (str(truth[0]), str(truth[1])))

    def test_cross_database_join_and_with(self):
        q = ("SELECT o.status, COUNT(*) FROM shop.orders o JOIN analytics.app_users u ON u.id = o.customer_id "
             "GROUP BY o.status")
        self.assertGreaterEqual(j(srv.execute_query(query=q))["row_count"], 1)
        self.assertEqual(j(srv.execute_query(query="WITH x AS (SELECT 1 AS a) SELECT a FROM x"))["rows"], [[1]])

    def test_caps(self):
        r = j(srv.execute_query(query="SELECT a.id FROM order_items a, order_items b", max_rows=50))
        self.assertEqual((r["row_count"], r["truncated"]), (50, "row limit 50"))
        r = j(srv.execute_query(query=SPEC.repeat, max_rows=1000))
        self.assertIn("size limit", r["truncated"])
        self.assertLess(r["row_count"], 1000)
        self.assertTrue(j(srv.execute_query(query=SPEC.long_cell))["rows"][0][0].endswith("...(+4500 chars)"))

    def test_callers_row_limits_are_respected_exactly(self):
        for q, ids in SPEC.limit_forms:
            with self.subTest(q=q):
                r = j(srv.execute_query(query=q))
                self.assertEqual([row[0] for row in r["rows"]], ids)
                self.assertNotIn("truncated", r)
        r = j(srv.execute_query(query="SELECT id FROM orders ORDER BY id LIMIT 5000", max_rows=10))
        self.assertEqual(([row[0] for row in r["rows"]], r["truncated"]), (list(range(1, 11)), "row limit 10"))

    def test_explain(self):
        self.assertIn(SPEC.explain_expect, srv.explain_query(query="SELECT * FROM orders WHERE id = 5"))
        with self.assertRaises(ToolError):
            srv.explain_query(query="DELETE FROM orders")

    def test_schema_resource(self):
        text = srv.schema_overview("shop", "shop")
        self.assertIn("orders(", text)
        if SPEC.has_foreign_keys:
            self.assertIn("FK->customers.id", text)

    def test_unknown_profile_lists_the_others(self):
        with self.assertRaisesRegex(ToolError, "Available: shop, analytics"):
            srv.execute_query(query="SELECT 1", profile="nope")


class Guard(Base):
    def test_refusals_carry_a_reason(self):
        for q in SPEC.common_refusals + SPEC.family_refusals + SPEC.engine_refusals:
            with self.subTest(q=q):
                with self.assertRaises(ToolError) as cm:
                    srv.execute_query(query=q)
                self.assertGreater(len(str(cm.exception)), 20)

    def test_show_describe_explain_still_work(self):
        if not SPEC.mysql_family:
            self.skipTest("SHOW/DESCRIBE through execute_query are MySQL-family only")
        self.assertIn("orders", srv.execute_query(query="SHOW TABLES"))
        self.assertIn("customer_id", srv.execute_query(query="DESCRIBE orders"))
        self.assertIn("orders", srv.execute_query(query="EXPLAIN SELECT * FROM orders"))


class EngineLayers(Base):
    """Layers 2 and 3 WITHOUT the guard (run() is called directly)."""
    def test_layer3_select_only_account(self):
        # A plain connection as the test account, without sql-mcp: the database itself must refuse the write.
        with self.assertRaisesRegex(Exception, SPEC.denied):
            SPEC.ro_sql("DELETE FROM orders WHERE id = -1")

    def test_layer2_read_only_session_stops_a_writer(self):
        if not SPEC.read_only_session:
            self.skipTest("this engine has NO read-only mode: see test_documented_gap_* (README warns)")
        before = SPEC.admin_sql("SELECT COUNT(*), SUM(total) FROM shop.orders")[0]
        # Harmless even if layer 2 were broken: no row matches, and a temporary table lives only in the session.
        for sql in ["DELETE FROM orders WHERE id = -1", "CREATE TEMPORARY TABLE tmp_x (i int)",
                    "UPDATE orders SET total = total WHERE id = -1"]:
            with self.subTest(sql=sql), self.assertRaisesRegex(ToolError, "read-only"):
                run(get_profile("writer"), sql)
        self.assertEqual(SPEC.admin_sql("SELECT COUNT(*), SUM(total) FROM shop.orders")[0], before)

    def test_documented_gap_engine_does_not_stop_a_writer(self):
        """StarRocks: no read-only session. A write-capable account's (harmless) DELETE succeeds through the
        server's own session; only the guard (and the account) stop it. If this ever fails, update the README."""
        if SPEC.read_only_session:
            self.skipTest("this engine enforces read-only")
        self.assertEqual(run(get_profile("writer"), "DELETE FROM orders WHERE id = -1")["columns"], [])
        with self.assertRaises(ToolError):
            srv.execute_query(query="DELETE FROM orders WHERE id = -1", profile="writer")

    def test_documented_gap_select_only_account_can_export(self):
        """StarRocks: a SELECT-only account can write files with INSERT INTO FILES (plain connection, no
        sql-mcp). Only the guard refuses it. If this ever fails, StarRocks closed the gap: update the README."""
        if SPEC.engine != "starrocks":
            self.skipTest("StarRocks only")
        self.assertGreater(SPEC.export_as_select_only_user(), 0)
        with self.assertRaises(ToolError):
            srv.execute_query(query=f"INSERT INTO FILES('path' = 'file://{SPEC.export_dir}', 'format' = 'csv') "
                                    "SELECT id FROM orders")

    def test_engine_specific_escapes_are_refused_without_the_guard(self):
        if not SPEC.unguarded_escapes:
            self.skipTest("no engine-specific escapes listed for this engine")
        for sql, path in SPEC.unguarded_escapes:
            with self.subTest(sql=sql):
                if path and os.path.exists(path):
                    os.remove(path)
                with self.assertRaisesRegex(ToolError, "read-only|no such function"):   # writefile() is CLI-only
                    run(get_profile("writer"), sql)
                if path:
                    self.assertFalse(os.path.exists(path), f"{path} was created")
        self.assertEqual(run(get_profile("writer"), "SELECT COUNT(*) FROM analytics.app_users")["rows"], [[3]])

    def test_sql_cannot_switch_read_only_off_for_later_calls(self):
        """PostgreSQL: a SELECT can call set_config() (the guard refuses it, but run() skips the guard). Each
        statement runs in its own READ ONLY transaction that is rolled back, which undoes the change."""
        if SPEC.engine != "postgres":
            self.skipTest("PostgreSQL only: set_config() exists there (MySQL/MariaDB: SET, refused by layer 1)")
        p = get_profile("writer")
        run(p, "SELECT set_config('default_transaction_read_only', 'off', false)")
        with self.assertRaisesRegex(ToolError, "read-only|read-write mode"):
            run(p, "SELECT set_config('transaction_read_only', 'off', true)")
        with self.assertRaisesRegex(ToolError, "read-only"):
            run(p, "DELETE FROM orders WHERE id = -1")

    def test_session_reads_text_like_the_guard(self):            # REVIEW #8
        self.assertTrue(SPEC.pin_check(lambda sql: one_value("one", sql)))


class TimeLimits(Base):
    def test_engine_timeout_is_readable_and_keeps_the_connection(self):
        sid = session_id()
        t0 = time.monotonic()
        with self.assertRaisesRegex(ToolError, "5000 ms limit"):
            run(get_profile("one"), SPEC.heavy)
        self.assertLess(4, time.monotonic() - t0)
        self.assertLess(time.monotonic() - t0, 15)
        self.assertEqual(session_id(), sid)

    def test_nothing_keeps_running_after_a_time_limit(self):      # REVIEW #3, without the guard
        t0 = time.monotonic()
        try:
            with self.assertRaisesRegex(ToolError, "ms limit"):
                run(get_profile("one"), SPEC.unguarded_slow)
            self.assertLess(time.monotonic() - t0, 25)
            time.sleep(0.5)
            self.assertEqual(SPEC.running_queries(), [])
        finally:
            for i in SPEC.running_queries():
                SPEC.kill(i)


class Pooling(Base):
    def test_reuse(self):
        sid = session_id()
        for _ in range(40):
            run(get_profile("one"), "SELECT 1")
        self.assertEqual(session_id(), sid)

    def test_truncation_and_errors_keep_the_connection(self):     # REVIEW #6
        sid = session_id()
        for _ in range(10):
            srv.execute_query(query="SELECT a.id FROM order_items a, order_items b", max_rows=10, profile="one")
            with self.assertRaises(ToolError):
                run(get_profile("one"), "SELECT no_such_column FROM orders")
        self.assertEqual(session_id(), sid)
        self.assertEqual(run(get_profile("one"), "SELECT 7")["rows"], [[7]])     # not stuck in a failed transaction

    def test_huge_result_streams_fast(self):
        for max_rows, label in [(10, "capped by LIMIT"), (10, "streamed")]:
            with self.subTest(label=label):
                sid = session_id()
                q = SPEC.big if label == "capped by LIMIT" else SPEC.big + " LIMIT 700000"
                t0 = time.monotonic()
                r = j(srv.execute_query(query=q, max_rows=max_rows, profile="one"))
                self.assertEqual(r["row_count"], 10)
                self.assertLess(time.monotonic() - t0, 2)
                if label == "capped by LIMIT":
                    self.assertEqual(session_id(), sid)

    def test_concurrency_queues_within_pool_size(self):
        ids, errors = set(), []
        lock = threading.Lock()

        def worker():
            try:
                r = run(get_profile("shop"), SPEC.concurrency)
                with lock:
                    ids.add(r["rows"][0][0])
            except Exception as e:      # noqa: BLE001
                errors.append(repr(e))
        worker()                                  # one call alone: how long a "wave" takes on this machine
        t0 = time.monotonic()
        worker()
        single = time.monotonic() - t0
        self.assertGreater(single, 0.1, "the 'slow' call was not slow: this test would prove nothing")
        ids.clear()
        t0 = time.monotonic()
        threads = [threading.Thread(target=worker) for _ in range(12)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        elapsed = time.monotonic() - t0
        self.assertEqual(errors, [])
        self.assertLessEqual(len(ids), 3)
        # 12 calls through 3 connections = about 4 waves: not all at once (< 3.3x), not one by one (> 8x)
        self.assertTrue(3.3 * single < elapsed < 8 * single, f"single {single:.2f}s, 12 calls {elapsed:.2f}s")

    def test_killed_connection_is_retried_once(self):
        if not SPEC.has_server:
            self.skipTest("in-process engine: no server session to kill")
        SPEC.kill(session_id())
        time.sleep(0.3)
        self.assertEqual(run(get_profile("one"), "SELECT 42")["rows"], [[42]])

    def test_waiter_is_woken_when_a_connection_is_discarded(self):    # REVIEW #1
        res = {}

        def holder():
            run(get_profile("one"), SPEC.slow_two_rows, max_rows=1)

        def waiter():
            t0 = time.monotonic()
            run(get_profile("one"), "SELECT 1")
            res["t"] = time.monotonic() - t0
        a = threading.Thread(target=holder)
        a.start()
        time.sleep(0.2)
        b = threading.Thread(target=waiter)
        b.start()
        a.join()
        b.join()
        self.assertLess(res.get("t", 99), 3)

    def test_no_database_means_no_database(self):                     # REVIEW #2
        p = get_profile("nodb")
        self.assertEqual(run(p, SPEC.current_namespace, database="analytics")["rows"], [[SPEC.namespace_value("analytics")]])
        self.assertEqual(run(p, SPEC.current_namespace)["rows"], [[SPEC.no_namespace_value]])


class Masking(Base):                                                    # REVIEW #4
    def test_derived_values_are_masked(self):
        for q in ["SELECT api_token AS x FROM app_users", "SELECT CONCAT(api_token, '') AS x FROM app_users",
                  "SELECT 'n/a' AS x UNION ALL SELECT api_token FROM app_users",
                  "SELECT t.x FROM (SELECT api_token AS x FROM app_users) t"]:
            with self.subTest(q=q):
                vals = [r[0] for r in j(srv.execute_query(query=q, profile="analytics"))["rows"]]
                self.assertFalse([v for v in vals if "tok_" in str(v)], vals)

    def test_oracles_and_error_channel(self):
        with self.assertRaisesRegex(ToolError, "masked column"):
            srv.execute_query(query="SELECT id FROM app_users WHERE api_token LIKE 'tok_a%'", profile="analytics")
        with self.assertRaises(ToolError) as cm:
            srv.execute_query(query=SPEC.error_channel, profile="analytics")
        self.assertNotIn("tok_", str(cm.exception))
        self.assertIn("hidden", str(cm.exception))


class Identifiers(Base):                                                # REVIEW #12
    def test_awkward_names(self):
        tables = [r[0] for r in j(srv.list_tables(database="odd-names"))["rows"]]
        self.assertEqual(sorted(tables), sorted(["order-archive", "Order History", "café"]))
        for t in tables:
            with self.subTest(t=t):
                self.assertTrue(j(srv.describe_table(table=t, database="odd-names"))["columns"][0].lower().startswith("id int"))
                self.assertGreaterEqual(j(srv.sample_rows(table=t, database="odd-names"))["row_count"], 1)
        self.assertEqual(j(srv.execute_query(query=SPEC.cafe_query, database="odd-names"))["rows"], [["brûlée"]])
        self.assertIn("café(", srv.schema_overview("shop", "odd-names"))


class SchemaCap(Base):                                                  # REVIEW #5
    def test_resource_is_complete_or_says_what_is_missing(self):
        text = srv.schema_overview("shop", SPEC.cap_namespace)
        truth = SPEC.cap_truth()
        lines = text.splitlines()
        missing = int(lines[-1].split()[1]) if lines[-1].startswith("... ") else 0
        self.assertEqual(len(lines) - (1 if missing else 0) + missing, truth)


class Tls(Base):                                                        # REVIEW #7
    def setUp(self):
        if not SPEC.has_server:
            self.skipTest("in-process engine: no network, no TLS")
        super().setUp()

    def cipher(self, profile):
        return SPEC.session_cipher(profile)

    def test_disable_is_plaintext(self):
        self.assertEqual(self.cipher("tls_disable"), "")

    def test_require_encrypts_or_refuses(self):
        if SPEC.has_tls():
            self.assertNotEqual(self.cipher("tls_require"), "")
        else:
            with self.assertRaisesRegex(ToolError, "SSL"):
                self.cipher("tls_require")

    def test_verify_full_succeeds_with_the_right_ca_and_fails_with_a_wrong_one(self):
        if not SPEC.tls_ca:
            self.skipTest("no CA for this test server")
        if not WRONG_CA.exists():
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-subj", "/CN=Wrong CA", "-keyout", str(WRONG_CA.with_suffix(".key")),
                            "-out", str(WRONG_CA)], check=True, capture_output=True)
        self.assertNotEqual(self.cipher("tls_verify_full"), "")
        with self.assertRaises(ToolError):
            self.cipher("tls_wrong_ca")


class OverStdio(unittest.TestCase):
    """The `sql-mcp` command itself, driven over real stdio JSON-RPC."""
    def test_protocol(self):
        exe = pathlib.Path(sys.executable).parent / "sql-mcp"
        p = subprocess.Popen([str(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, env=dict(os.environ))
        n = 0

        def call(method, params=None):
            nonlocal n
            n += 1
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": n, "method": method, "params": params or {}}) + "\n")
            p.stdin.flush()
            reply = json.loads(p.stdout.readline())        # fails loudly if stdout is ever polluted
            self.assertEqual(reply["id"], n)
            return reply["result"]
        try:
            init = call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "conformance", "version": "0"}})
            self.assertEqual(init["serverInfo"]["name"], "sql-mcp")
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            p.stdin.flush()
            tools = call("tools/list")["tools"]
            self.assertEqual(len(tools), 8)
            self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in tools))
            r = call("tools/call", {"name": "execute_query", "arguments": {"query": "SELECT COUNT(*) AS n FROM orders"}})
            self.assertEqual(json.loads(r["content"][0]["text"])["rows"], [[40]])
            r = call("tools/call", {"name": "execute_query", "arguments": {"query": "DROP TABLE orders"}})
            self.assertTrue(r["isError"])
            tmpl = call("resources/templates/list")["resourceTemplates"]
            self.assertEqual(tmpl[0]["uriTemplate"], "schema://{profile}/{database}")
            self.assertEqual({x["name"] for x in call("prompts/list")["prompts"]}, {"explore_database", "investigate"})
        finally:
            p.stdin.close()
            p.wait(timeout=10)
            p.stdout.close()


def setUpModule():
    print(f"\n[conformance] engine={ENGINE} port={PORT} server={SPEC.version()}", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
