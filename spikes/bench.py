"""Phase 3 spike harness: the same tests through both adapters, on every engine.

    spikes/.venv/bin/python spikes/bench.py [test ...]
"""
import os
import pathlib
import sqlite3
import sys
import time
import tracemalloc

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import adapters_raw  # noqa: E402
import adapters_sa  # noqa: E402

SQLITE = HERE / "shop.sqlite"
RO = dict(user="readonly_user", password="readonly-pw-test-only")
RW = dict(user="app_writer", password="writer-pw-test-only")
CFG = {
    "mysql": dict(host="127.0.0.1", port=33316, database="shop"),
    "mariadb": dict(host="127.0.0.1", port=33326, database="shop"),
    "postgres": dict(host="127.0.0.1", port=54329, database="app"),
    "sqlite": dict(path=str(SQLITE)),
}
T = {"mysql": "order_items", "mariadb": "order_items", "postgres": "shop.order_items", "sqlite": "order_items"}
SCHEMA = {"mysql": "shop", "mariadb": "shop", "postgres": "shop", "sqlite": "main"}


def make_sqlite():
    if SQLITE.exists():
        return
    c = sqlite3.connect(SQLITE)
    c.executescript("""
      PRAGMA foreign_keys = ON;
      CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL);
      CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
                           status TEXT NOT NULL, total NUMERIC NOT NULL);
      CREATE TABLE order_items (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
                                quantity INTEGER NOT NULL);
      CREATE TABLE "order-archive" (id INTEGER PRIMARY KEY, note TEXT);
      CREATE TABLE app_users (id INTEGER PRIMARY KEY, email TEXT, api_token TEXT);
    """)
    c.executemany("INSERT INTO customers VALUES (?,?,?)", [(i, f"C{i}", f"c{i}@example.test") for i in range(1, 11)])
    c.executemany("INSERT INTO orders VALUES (?,?,?,?)", [(i, 1 + i % 10, "paid", 10 * i) for i in range(1, 41)])
    c.executemany("INSERT INTO order_items VALUES (?,?,?)", [(i, 1 + i % 40, 1 + i % 3) for i in range(1, 91)])
    c.execute("INSERT INTO app_users VALUES (1, 'ann@example.test', 'tok_ann_0123456789abcdef012345')")
    c.commit()
    c.close()


def adapter(kind, engine, creds=RO, **extra):
    cfg = dict(CFG[engine], **({} if engine == "sqlite" else creds), **extra)
    return adapters_raw.DIALECTS[engine](cfg) if kind == "raw" else adapters_sa.Adapter(engine, cfg)


def err(fn):
    try:
        r = fn()
        return f"NO ERROR ({str(r)[:60]})"
    except Exception as e:     # noqa: BLE001 - we report every failure type
        return f"{type(e).__name__}: {str(e).splitlines()[0][:110]}"


def t_select1():
    print("\n== 200 sequential SELECT 1 (pooled)")
    for engine in CFG:
        for kind in ("raw", "sa"):
            a = adapter(kind, engine)
            a.query("SELECT 1")
            t0 = time.perf_counter()
            for _ in range(200):
                a.query("SELECT 1")
            print(f"  {engine:<9} {kind:<4} {(time.perf_counter() - t0) / 200 * 1000:6.2f} ms/call")


def t_big():
    print("\n== 729,000-row cross join capped to 100 rows: time, Python peak memory")
    for engine in CFG:
        sql = f"SELECT a.id FROM {T[engine]} a, {T[engine]} b, {T[engine]} c"
        variants = [("raw stream+discard", "raw", dict(), "discard"), ("raw stream+drain", "raw", dict(), "drain"),
                    ("raw LIMIT injected", "raw", dict(inject_limit=True), "drain"),
                    ("sa  result.close()", "sa", dict(), None), ("sa  invalidate()", "sa", dict(invalidate_on_early_stop=True), None)]
        for label, kind, kw, close in variants:
            a = adapter(kind, engine)
            if close:
                a.stream_close = close
            a.query("SELECT 1")
            tracemalloc.start()
            t0 = time.perf_counter()
            r = a.query(sql, 100, **kw)
            dt = time.perf_counter() - t0
            peak = tracemalloc.get_traced_memory()[1] / 1e6
            tracemalloc.stop()
            t1 = time.perf_counter()
            a.query("SELECT 1")
            print(f"  {engine:<9} {label:<20} {dt:6.3f}s  peak {peak:6.1f} MB  rows={len(r['rows'])} "
                  f"truncated={r['truncated']!r}  next call {1000 * (time.perf_counter() - t1):.1f} ms")


def write(a, sql):
    """Run a statement through a PLAIN cursor, with the adapter's own session/transaction setup."""
    if isinstance(a, adapters_sa.Adapter):
        with a.engine.connect() as c:
            try:
                return c.exec_driver_sql(sql).rowcount
            finally:
                c.rollback()
    conn, born = a.pool.acquire()
    try:
        cur = conn.cursor()
        cur.execute(sql)
        return cur.rowcount
    finally:
        if hasattr(conn, "rollback"):
            conn.rollback()
        a.pool.release(conn, born)


def t_readonly():
    print("\n== layer 2 alone (no guard): a WRITER account tries to write (plain cursor)")
    for engine in ("mysql", "mariadb", "postgres", "sqlite"):
        tbl = "shop.orders" if engine == "postgres" else "orders"
        for kind in ("raw", "sa"):
            a = adapter(kind, engine, RW)
            print(f"  {engine:<9} {kind:<4} DELETE -> {err(lambda: write(a, f'DELETE FROM {tbl} WHERE id = -1'))}")
            if engine in ("mysql", "mariadb"):
                print(f"  {engine:<9} {kind:<4} CREATE TEMPORARY TABLE -> {err(lambda: write(a, 'CREATE TEMPORARY TABLE tmp_x (i int)'))}")


def t_pg_bypass():
    print("\n== PostgreSQL: can SQL switch layer 2 off? (writer, pool_size 1, plain cursor)")
    for kind in ("raw", "sa"):
        a = adapter(kind, "postgres", RW, pool_size=1)
        same_txn = err(lambda: write(a, "SELECT set_config('transaction_read_only', 'off', true)"))
        r1 = err(lambda: write(a, "SELECT set_config('default_transaction_read_only', 'off', false)"))
        r2 = err(lambda: a.query("SELECT current_setting('default_transaction_read_only'), current_setting('transaction_read_only')"))
        r3 = err(lambda: write(a, "DELETE FROM shop.orders WHERE id = -1"))
        print(f"  {kind:<4} set_config(transaction_read_only) in the same txn -> {same_txn[:90]}")
        print(f"       set_config(default_..., session) -> {r1[:30]}; next call sees -> {r2[60:140]}")
        print(f"       then DELETE -> {r3[:100]}")


def t_timeout():
    print("\n== server/engine timeout = 1000 ms on a heavy query")
    for engine in CFG:
        t = T[engine]
        heavy = (f"SELECT COUNT(*) FROM {t} a, {t} b, {t} c, {t} d WHERE (a.id + b.id + c.id + d.id) % 7 = 0"
                 if engine != "sqlite" else
                 "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 1000000000) SELECT COUNT(*) FROM n")
        for kind in ("raw", "sa"):
            a = adapter(kind, engine, timeout_ms=1000)
            t0 = time.perf_counter()
            e = err(lambda: a.query(heavy))
            print(f"  {engine:<9} {kind:<4} {time.perf_counter() - t0:5.2f}s  {e[:100]}")
    a = adapter("raw", "mariadb", timeout_ms=1000)
    t0 = time.perf_counter()
    e = err(lambda: a.query("SHOW DATABASES WHERE (SELECT COUNT(*) FROM order_items a, order_items b, order_items c, "
                            "order_items d WHERE (a.id + b.id + c.id + d.id) % 7 = 0) >= 0"))
    print(f"  mariadb   raw  SHOW ... WHERE subquery: {time.perf_counter() - t0:5.2f}s  {e[:90]}")


def t_reflect():
    print("\n== reflection: describe(orders) and a hyphenated table")
    for engine in CFG:
        for kind in ("raw", "sa"):
            a = adapter(kind, engine)
            a.query("SELECT 1")
            t0 = time.perf_counter()
            d = a.describe(SCHEMA[engine], "orders")
            dt = (time.perf_counter() - t0) * 1000
            tabs = a.tables(SCHEMA[engine])
            hy = err(lambda: a.describe(SCHEMA[engine], "order-archive")["columns"]) if "order-archive" in tabs else "n/a (not in this seed)"
            print(f"  {engine:<9} {kind:<4} {dt:7.1f} ms  cols={len(d['columns'])} fk_out={d['fk_out']} "
                  f"fk_in={d['fk_in']}  order-archive: {hy[:50]}")


def t_sqlite_guards():
    print("\n== SQLite layer 2 without the guard (authorizer + mode=ro + query_only)")
    probe = HERE / "attached.db"
    for kind in ("raw", "sa"):
        a = adapter(kind, "sqlite")
        for q in [f"ATTACH DATABASE '{probe}' AS x", "PRAGMA query_only = OFF", "DELETE FROM orders",
                  "CREATE TABLE t2 (x)", "SELECT load_extension('x')", "VACUUM INTO '/tmp/sqlmcp-copy.db'"]:
            print(f"  {kind:<4} {q[:40]:<40} -> {err(lambda: a.query(q))[:90]}")
    print(f"  attached.db created: {probe.exists()}   /tmp/sqlmcp-copy.db created: {os.path.exists('/tmp/sqlmcp-copy.db')}")


if __name__ == "__main__":
    make_sqlite()
    tests = {"select1": t_select1, "big": t_big, "readonly": t_readonly, "pgbypass": t_pg_bypass,
             "timeout": t_timeout, "reflect": t_reflect, "sqliteguards": t_sqlite_guards}
    for name in sys.argv[1:] or tests:
        tests[name]()
