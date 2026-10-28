"""Spike: StarRocks through PyMySQL (MySQL protocol). Same questions as for the other engines.

    spikes/.venv/bin/python spikes/spike_starrocks.py
"""
import sys
import time

import pymysql
from pymysql.cursors import SSCursor

sys.path.insert(0, "spikes")
from spike_sqlglot import classify  # noqa: E402

HOST, PORT = "127.0.0.1", 39030
RO = dict(user="readonly_user", password="readonly-pw-test-only")
RW = dict(user="app_writer", password="writer-pw-test-only")
HEAVY = ("SELECT COUNT(*) FROM shop.order_items a, shop.order_items b, shop.order_items c, shop.order_items d, "
         "shop.order_items e WHERE (a.id + b.id + c.id + d.id + e.id) % 7 = 0")


def conn(creds, init=None, **kw):
    return pymysql.connect(host=HOST, port=PORT, database="shop", autocommit=True, init_command=init, **creds, **kw)


def run(c, sql, cursor=None):
    try:
        with c.cursor(cursor) as k:
            k.execute(sql)
            return k.fetchmany(3) if k.description else f"ok rowcount={k.rowcount}"
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"[:160]


def timed(c, sql):
    t0 = time.perf_counter()
    r = run(c, sql)
    return f"{time.perf_counter() - t0:6.2f}s  {str(r)[:120]}"


print("== version / session variables")
c = conn(RO)
print("  ", run(c, "SELECT current_version()"))
print("  ", run(c, "SHOW VARIABLES WHERE Variable_name IN ('transaction_read_only','tx_read_only','read_only',"
                   "'query_timeout','max_execution_time','sql_mode')"))

print("\n== layer 2: does a read-only session block a WRITER?")
for init in ["SET SESSION transaction_read_only=1", "SET SESSION tx_read_only=1"]:
    try:
        w = conn(RW, init)
        print(f"  init {init!r}: connect ok; DELETE -> {run(w, 'DELETE FROM shop.orders WHERE id = -1')}")
        print(f"     INSERT -> {run(w, 'INSERT INTO shop.orders VALUES (-1, 1, \'x\', 0, now())')}")
        print(f"     cleanup as writer -> {run(conn(RW), 'DELETE FROM shop.orders WHERE id = -1')}")
    except Exception as e:  # noqa: BLE001
        print(f"  init {init!r}: CONNECT FAILED {type(e).__name__}: {e}")
w = conn(RW)
print("  START TRANSACTION READ ONLY ->", run(w, "START TRANSACTION READ ONLY"))

print("\n== layer 3: SELECT-only user")
for q in ["DELETE FROM shop.orders WHERE id = -1", "INSERT INTO shop.orders VALUES (-1, 1, 'x', 0, now())",
          "INSERT INTO FILES('path' = 'file:///tmp/sqlmcp_sr/', 'format' = 'csv') SELECT * FROM shop.orders",
          "SELECT * FROM shop.orders INTO OUTFILE 'file:///tmp/sqlmcp_sr_out/' FORMAT AS CSV",
          "CACHE SELECT * FROM shop.orders", "SUBMIT TASK AS INSERT INTO shop.orders SELECT * FROM shop.orders",
          "CREATE TABLE shop.t2 (i INT) DISTRIBUTED BY HASH(i) PROPERTIES ('replication_num'='1')",
          "SET query_timeout = 3600", "SET GLOBAL query_timeout = 3600"]:
    print(f"  {q[:62]:<62} -> {str(run(conn(RO), q))[:110]}")

print("\n== timeout: query_timeout = 1 (seconds) on a 5.9e9-combination cross join")
c = conn(RO, "SET SESSION query_timeout = 1")
print("  plain              ", timed(c, HEAVY))
print("  SET_VAR hint 60 s  ", timed(conn(RO, "SET SESSION query_timeout = 1"),
                                     HEAVY.replace("SELECT", "SELECT /*+ SET_VAR(query_timeout = 60) */", 1)))
print("  SLEEP(3)           ", timed(conn(RO, "SET SESSION query_timeout = 1"), "SELECT SLEEP(3)"))
print("  SHOW ... WHERE sub ", timed(conn(RO, "SET SESSION query_timeout = 1"),
                                     "SHOW DATABASES WHERE (" + HEAVY.replace("SELECT COUNT(*)", "SELECT COUNT(*)") + ") >= 0"))

print("\n== streaming / truncation (729,000 rows capped to 100)")
BIG = "SELECT a.id FROM shop.order_items a, shop.order_items b, shop.order_items c"
for label, sql, close in [("SSCursor + discard", BIG, "discard"), ("SSCursor + drain", BIG, "drain"),
                          ("LIMIT 101 injected", BIG + " LIMIT 101", "drain")]:
    c = conn(RO, "SET SESSION query_timeout = 30")
    t0 = time.perf_counter()
    k = c.cursor(SSCursor)
    k.execute(sql)
    rows = k.fetchmany(101)
    if close == "drain":
        k.close()
    else:
        c.close()
    print(f"  {label:<20} {time.perf_counter() - t0:6.3f}s rows={len(rows)}")

print("\n== introspection")
c = conn(RO)
for q in ["SELECT table_name, table_rows, data_length FROM information_schema.tables WHERE table_schema='shop'",
          "SELECT column_name, column_type, column_key FROM information_schema.columns WHERE table_schema='shop' AND table_name='orders'",
          "SELECT COUNT(*) FROM information_schema.key_column_usage WHERE table_schema='shop'",
          "SELECT COUNT(*) FROM information_schema.referential_constraints"]:
    print(f"  {q[:70]:<70} -> {str(run(c, q))[:100]}")

print("\n== sqlglot 'starrocks' dialect")
for q in ["SELECT 1", "SELECT /*+ SET_VAR(query_timeout = 60) */ 1", "SELECT * FROM shop.orders LIMIT 5",
          "SELECT * FROM default_catalog.shop.orders", "SELECT SLEEP(3)", "CACHE SELECT * FROM shop.orders",
          "INSERT INTO FILES('path' = 's3://x/', 'format' = 'csv') SELECT 1", "SELECT * FROM TABLE(generate_series(1, 3))",
          "SUBMIT TASK AS INSERT INTO t SELECT 1"]:
    print(f"  {classify(q, 'starrocks')[:60]:<60} | {q}")

print("\n== latency: 200 x SELECT 1 on one connection")
c = conn(RO)
run(c, "SELECT 1")
t0 = time.perf_counter()
for _ in range(200):
    run(c, "SELECT 1")
print(f"  {(time.perf_counter() - t0) / 200 * 1000:.2f} ms/call")
