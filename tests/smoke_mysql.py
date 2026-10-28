"""End-to-end test of mysql_server.py against the Docker test database.

    docker compose up -d --wait
    .venv/bin/python tests/smoke_mysql.py

Part A drives the server over REAL stdio JSON-RPC (what Claude Code does).
Part B imports the module to test things the tools deliberately cannot do (writes, pool, kill).
Ground truth always comes from a separate root connection (docker exec), never from the server.
Test-only credentials; exits non-zero if any check fails.
"""
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCKER = shutil.which("docker") or os.path.expanduser("~/.docker/bin/docker")
CONTAINER = "sql-mcp-mysql"


def resolve_config() -> str:
    """connections.docker-test.json, with every profile's port replaced by SQL_MCP_MYSQL_PORT when that is set
    (shell env first, then .env, the same precedence docker compose uses), so the port lives in one place."""
    port = os.environ.get("SQL_MCP_MYSQL_PORT")
    env_file = ROOT / ".env"
    if port is None and env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.strip().startswith("SQL_MCP_MYSQL_PORT="):
                port = line.split("=", 1)[1].strip()
    src = ROOT / "connections.docker-test.json"
    cfg = json.loads(src.read_text())
    if port is None or all(p["port"] == int(port) for p in cfg["profiles"].values()):
        return str(src)
    for p in cfg["profiles"].values():
        p["port"] = int(port)
    out = ROOT / ".smoke" / "connections.json"      # git-ignored; never writes outside the project
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(cfg))
    return str(out)


ENV = dict(os.environ, MYSQL_CONFIG=resolve_config(),
           TEST_RO_PASSWORD="readonly-pw-test-only", TEST_RW_PASSWORD="writer-pw-test-only",
           MYSQL_CACHE_TTL="0")      # no caching, so every check hits the database
FAILS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def ro_connections() -> int:
    """Total connections ever opened by readonly_user (exact: ignores the health check, my root queries, other users)."""
    return int(root_sql("SELECT COALESCE(SUM(total_connections),0) FROM performance_schema.accounts WHERE user='readonly_user'"))


def root_sql(sql: str) -> str:
    out = subprocess.run([DOCKER, "exec", CONTAINER, "mysql", "-uroot", "-prootpw-test-only", "-N", "-B", "-e", sql],
                         capture_output=True, text=True, check=True).stdout
    return out.strip()


class Client:
    """Minimal MCP stdio client: newline-delimited JSON-RPC over a child process."""
    def __init__(self):
        self.p = subprocess.Popen([sys.executable, str(ROOT / "mysql_server.py")], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=ENV)
        self.n = 0
        self.init = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                "clientInfo": {"name": "smoke", "version": "0"}})
        self.notify("notifications/initialized")

    def _send(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def notify(self, method):
        self._send({"jsonrpc": "2.0", "method": method})

    def request(self, method, params=None):
        self.n += 1
        self._send({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
        line = self.p.stdout.readline()            # json.loads raises if stdout is ever polluted
        reply = json.loads(line)
        assert reply["id"] == self.n, reply
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply["result"]

    def tool(self, name, **args):
        """Returns (is_error, text, parsed_json_or_None)."""
        r = self.request("tools/call", {"name": name, "arguments": args})
        text = r["content"][0]["text"]
        try:
            return r["isError"], text, json.loads(text)
        except ValueError:
            return r["isError"], text, None

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=10)


def part_a():
    print("\n=== Part A: over real stdio JSON-RPC ===")
    c = Client()
    check("initialize: name/version", c.init["serverInfo"] == {"name": "mysql-server", "version": "2.0.0"}, str(c.init["serverInfo"]))
    caps = c.init["capabilities"]
    check("capabilities: tools + resources + prompts", all(k in caps for k in ("tools", "resources", "prompts")), str(caps))

    tools = c.request("tools/list")["tools"]
    names = {t["name"] for t in tools}
    expected = {"list_profiles", "list_databases", "list_tables", "describe_table", "search_schema",
                "sample_rows", "execute_query", "explain_query"}
    check("tools/list: 8 tools", names == expected, str(names ^ expected))
    check("every tool is annotated read-only", all(t.get("annotations", {}).get("readOnlyHint") for t in tools))

    err, text, j = c.tool("list_profiles")
    check("list_profiles: 3 profiles, default shop", not err and j["default"] == "shop" and len(j["profiles"]) == 3, text[:120])
    check("list_profiles never leaks a password", "readonly-pw" not in text and "writer-pw" not in text)

    err, _, j = c.tool("list_databases")
    check("list_databases: shop+analytics, no system schemas", not err and {"shop", "analytics"} <= set(j) and "mysql" not in j, str(j))
    err, _, j = c.tool("list_databases", include_system=True)
    check("list_databases include_system", not err and "information_schema" in j, str(j))

    err, _, j = c.tool("list_tables")
    check("list_tables (default db shop): 4 tables", not err and [r[0] for r in j["rows"]] == ["customers", "order_items", "orders", "products"], str(j)[:200])
    err, _, j = c.tool("list_tables", database="analytics")
    check("list_tables database=analytics", not err and {r[0] for r in j["rows"]} == {"app_users", "page_views"}, str(j)[:200])

    err, _, j = c.tool("describe_table", table="orders")
    check("describe_table: columns", not err and any(x.startswith("id int NOT NULL PRI auto_increment") for x in j["columns"]), str(j)[:300])
    check("describe_table: FK out (references)", any("customer_id -> shop.customers.id" == x for x in j["references"]), str(j["references"]))
    check("describe_table: FK in (referenced_by)", any(x.startswith("shop.order_items.order_id") for x in j["referenced_by"]), str(j["referenced_by"]))
    check("describe_table: indexes", any(x.startswith("PRIMARY(id)") for x in j["indexes"]), str(j["indexes"]))
    err, text, _ = c.tool("describe_table", table="nope")
    check("describe_table unknown table -> readable error", err and "not found" in text, text)
    err, text, _ = c.tool("describe_table", table="orders`; DROP TABLE x; --")
    check("describe_table injection attempt rejected", err and "Invalid table name" in text, text)
    err, text, _ = c.tool("describe_table", table="orders", database="shop`; DROP")
    check("database injection attempt rejected", err and "Invalid database name" in text, text)

    err, _, j = c.tool("search_schema", keyword="email", database="analytics")
    check("search_schema email in analytics", not err and any("analytics.app_users.email" in x for x in j["columns"]), str(j))
    err, _, j = c.tool("search_schema", keyword="order")
    check("search_schema 'order' finds tables", not err and {"shop.orders", "shop.order_items"} <= set(j["tables"]), str(j))
    err, _, j = c.tool("search_schema", keyword="%")
    check("search_schema escapes LIKE wildcard (no match-all)", not err and j["tables"] == [] and j["columns"] == [], str(j)[:200])

    # masking
    err, _, j = c.tool("sample_rows", table="app_users", profile="analytics", limit=2)
    cols = j["columns"]
    row = dict(zip(cols, j["rows"][0]))
    check("masking: password_hash/api_token/email are ***", row["password_hash"] == row["api_token"] == row["email"] == "***", str(row))
    check("masking: reported in 'masked' and id not masked", set(j["masked"]) == {"password_hash", "api_token", "email"} and row["id"] == 1, str(j.get("masked")))
    err, _, j = c.tool("execute_query", query="SELECT api_token AS harmless_name FROM app_users", profile="analytics")
    check("masking survives an alias (original column name is checked)", not err and j["rows"][0] == ["***"] and j["masked"] == ["harmless_name"], str(j)[:200])
    err, _, j = c.tool("execute_query", query="SELECT CONCAT(api_token, '') AS harmless_name FROM app_users", profile="analytics")
    print("INFO  known limitation: expression + alias is NOT masked ->", "LEAKED" if j["rows"][0][0] != "***" else "masked", "(use column-level GRANTs for real protection)")
    err, _, j = c.tool("sample_rows", table="customers", limit=2)
    check("shop profile does not mask email (default mask list)", not err and "@example.test" in j["rows"][0][2], str(j["rows"][0]))
    err, _, j = c.tool("sample_rows", table="orders", limit=999)
    check("sample_rows limit clamped to 20", not err and j["row_count"] == 20, str(j.get("row_count")))

    # correctness vs ground truth
    truth = root_sql("SELECT COUNT(*), ROUND(SUM(total),2) FROM shop.orders WHERE DATE(created_at)=CURDATE()-INTERVAL 1 DAY").split("\t")
    err, _, j = c.tool("execute_query", query="SELECT COUNT(*) AS n, ROUND(SUM(total),2) AS revenue FROM orders WHERE DATE(created_at)=CURDATE()-INTERVAL 1 DAY")
    got = j["rows"][0]
    check("execute_query matches ground truth (yesterday)", not err and str(got[0]) == truth[0] and str(got[1]) == truth[1], f"{got} vs {truth}")
    err, _, j = c.tool("execute_query", query="SELECT o.status, COUNT(*) FROM shop.orders o JOIN analytics.app_users u ON u.id = o.customer_id GROUP BY o.status", database=None)
    check("cross-database query works (shop JOIN analytics)", not err and j["row_count"] >= 1, str(j)[:200])
    err, text, j = c.tool("execute_query", query="WITH x AS (SELECT 1 AS a) SELECT a FROM x")
    check("WITH ... SELECT allowed", not err and j["rows"] == [[1]], text)

    # caps
    err, _, j = c.tool("execute_query", query="SELECT a.id FROM order_items a, order_items b", max_rows=50)
    check("row cap: 50 rows + truncated flag", not err and j["row_count"] == 50 and j["truncated"] == "row limit 50", str({k: v for k, v in j.items() if k != 'rows'}))
    err, _, j = c.tool("execute_query", query="SELECT REPEAT('x', 400) AS t FROM order_items a, order_items b", max_rows=1000)
    check("size cap: stops before 1000 rows, flagged", not err and "size limit" in j.get("truncated", "") and j["row_count"] < 1000, str({k: v for k, v in j.items() if k != 'rows'}))
    err, _, j = c.tool("execute_query", query="SELECT REPEAT('y', 5000) AS t")
    check("cell cap: 5000-char value cut to 500 + note", not err and j["rows"][0][0].endswith("...(+4500 chars)"), j["rows"][0][0][-40:])
    start = time.time()
    err, _, j = c.tool("execute_query", query="SELECT a.id FROM order_items a, order_items b, order_items c", max_rows=10)
    check("huge result streams: 729,000-row cross join returns 10 rows in <2s", not err and j["row_count"] == 10 and time.time() - start < 2, f"{time.time()-start:.1f}s")

    # timeout (server-side max_execution_time = 5000 ms)
    start = time.time()
    err, text, _ = c.tool("execute_query", query="SELECT COUNT(*) FROM order_items a, order_items b, order_items c, order_items d, order_items e WHERE (a.id + b.id + c.id + d.id + e.id) % 7 = 0")
    check("timeout: a query that must evaluate 5.9B row combinations is killed at ~5s with a readable message", err and "5000 ms limit" in text and 4 < time.time() - start < 15, f"{time.time()-start:.1f}s | {text[:120]}")
    err, _, j = c.tool("execute_query", query="SELECT 1")
    check("server still healthy after the timeout", not err and j["rows"] == [[1]])

    # guard over the wire
    for label, q in [("DROP", "DROP TABLE orders"), ("comment", "SELECT 1 /* x */"), ("stacked", "SELECT 1; SELECT 2"),
                     ("sleep", "SELECT SLEEP(1)"), ("outfile", "SELECT 1 INTO OUTFILE '/tmp/x'"), ("explain analyze", "EXPLAIN ANALYZE SELECT 1")]:
        err, text, _ = c.tool("execute_query", query=q)
        check(f"guard blocks {label} with a reason", err and "Error executing tool" in text and len(text) > 40, text)
    err, text, _ = c.tool("execute_query", query="SELECT 1", profile="nope")
    check("unknown profile -> lists available ones", err and "Available: shop, analytics" in text, text)

    err, _, j = c.tool("explain_query", query="SELECT * FROM orders WHERE id = 5")
    check("explain_query returns a plan (key=PRIMARY)", not err and "PRIMARY" in json.dumps(j["rows"]), str(j)[:200])
    err, text, _ = c.tool("explain_query", query="SHOW TABLES")
    check("explain_query rejects non-SELECT", err and "takes a SELECT" in text, text)

    # the other two primitives
    tmpl = c.request("resources/templates/list")["resourceTemplates"]
    check("resource template advertised", any(t["uriTemplate"] == "schema://{profile}/{database}" for t in tmpl), str(tmpl))
    res = c.request("resources/read", {"uri": "schema://shop/shop"})["contents"][0]["text"]
    check("resource: whole schema incl. FK", "orders(" in res and "FK->customers.id" in res and "order_items(" in res, res[:200])
    prompts = {p["name"] for p in c.request("prompts/list")["prompts"]}
    check("prompts listed", prompts == {"explore_database", "investigate"}, str(prompts))
    msg = c.request("prompts/get", {"name": "investigate", "arguments": {"question": "top customer?"}})["messages"][0]["content"]["text"]
    check("prompt renders the question", "top customer?" in msg and "execute_query" in msg, msg[:80])

    # compare output size with the v1 shape for the same data (token efficiency)
    err, text_v2, _ = c.tool("execute_query", query="SELECT id, customer_id, status, total FROM orders", max_rows=40)
    v1_shape = json.dumps({"rows": [dict(zip(["id", "customer_id", "status", "total"], r)) for r in json.loads(text_v2)["rows"]], "truncated": False}, default=str)
    print(f"INFO  40-row result: v2 compact = {len(text_v2)} chars vs v1 dict-per-row shape = {len(v1_shape)} chars ({100 - 100*len(text_v2)//len(v1_shape)}% smaller)")
    c.close()


def load_module():
    for k in ("MYSQL_CONFIG", "TEST_RO_PASSWORD", "TEST_RW_PASSWORD", "MYSQL_CACHE_TTL"):
        os.environ[k] = ENV[k]
    spec = importlib.util.spec_from_file_location("ms", ROOT / "mysql_server.py")
    ms = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ms)
    return ms


def part_b():
    print("\n=== Part B: layers 2 and 3, pool, concurrency, kill/retry (imports the module) ===")
    ms = load_module()
    from mcp.server.mcpserver.exceptions import ToolError
    before = int(root_sql("SELECT COUNT(*) FROM shop.orders"))

    for label, profile, expect in [("layer 3 (SELECT-only user)", "shop", "command denied"),
                                   ("layer 2 (writer user, session is read-only)", "writer-session-test", "read-only")]:
        try:
            ms.run(ms.get_profile(profile), "DELETE FROM orders WHERE id = 1")
            check(label + " blocks DELETE", False, "DELETE succeeded!")
        except ToolError as e:
            check(label + " blocks DELETE", expect in str(e), str(e))
    check("orders untouched", int(root_sql("SELECT COUNT(*) FROM shop.orders")) == before)

    # pool reuse: 40 sequential calls should open at most pool_size connections, not 40
    p = ms.get_profile("shop")
    ms.run(p, "SELECT 1")                      # make sure the pool holds a connection first
    c0 = ro_connections()
    for _ in range(40):
        ms.run(p, "SELECT 1")
    c1 = ro_connections()
    check("pool reuse: 40 more calls opened exactly 0 new connections", c1 - c0 == 0, f"{c1 - c0} new connections")

    # concurrency beyond pool size: 12 threads, pool_size 3 -> they queue, none fail
    errors, c2 = [], ro_connections()

    def worker():
        try:
            ms.run(p, "SELECT SLEEP(0.3)")      # direct run() has no guard, so SLEEP is usable here
        except Exception as e:                  # noqa: BLE001 - we want to count every failure
            errors.append(repr(e))
    t0 = time.time()
    threads = [threading.Thread(target=worker) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    c3 = ro_connections()
    check("concurrency: 12 parallel calls, 0 errors", not errors, str(errors[:2]))
    check("concurrency: never more than pool_size(3) connections in total", c3 - c2 <= 2, f"{c3 - c2} new on top of 1 pooled")
    check("concurrency: queued, not serial (4 waves of 0.3s, not 12)", 1.0 < time.time() - t0 < 3.0, f"{time.time()-t0:.1f}s")

    # kill the pooled connection from outside, then call again: must transparently recover
    ms.run(p, "SELECT 1")
    ids = root_sql("SELECT id FROM information_schema.processlist WHERE user='readonly_user'").split()
    for i in ids:
        root_sql(f"KILL {i}")
    time.sleep(0.3)
    try:
        r = ms.run(p, "SELECT 42")
        check(f"kill/retry: server killed {len(ids)} connection(s); next call still succeeds", r["rows"] == [[42]], str(r))
    except ToolError as e:
        check("kill/retry", False, str(e))

    # wrong password -> clear error, no password in the message
    os.environ["TEST_RO_PASSWORD"] = "wrong"
    ms.config.cache_clear()
    ms._pools.clear()
    try:
        ms.run(ms.get_profile("shop"), "SELECT 1")
        check("wrong password rejected", False)
    except ToolError as e:
        check("wrong password: clear error, password not leaked", "Access denied" in str(e) and "wrong" not in str(e).replace("Access denied", ""), str(e))


if __name__ == "__main__":
    part_a()
    part_b()
    print(f"\n{'ALL CHECKS PASSED' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)
