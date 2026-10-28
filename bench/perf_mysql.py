"""Performance baseline for the MySQL path. Run the SAME script before and after a change.

    .venv/bin/python bench/perf_mysql.py [path/to/module.py]     # default: mysql_server.py

Needs the Docker test stack (docker compose up -d --wait). Prints one line per measurement.
"""
import importlib.util
import os
import pathlib
import resource
import sys
import time
import tracemalloc

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from smoke_mysql import resolve_config, root_sql  # noqa: E402  (same config + ground-truth helpers as the smoke test)

os.environ.update(MYSQL_CONFIG=resolve_config(), TEST_RO_PASSWORD="readonly-pw-test-only",
                  TEST_RW_PASSWORD="writer-pw-test-only", MYSQL_CACHE_TTL="0")
path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "mysql_server.py")
spec = importlib.util.spec_from_file_location("target", path)
ms = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ms)


def ro_connections() -> int:
    return int(root_sql("SELECT COALESCE(SUM(total_connections),0) FROM performance_schema.accounts WHERE user='readonly_user'"))


p = ms.get_profile("shop")
ms.run(p, "SELECT 1")

t0 = time.perf_counter()
for _ in range(200):
    ms.run(p, "SELECT 1")
print(f"run('SELECT 1')            : {(time.perf_counter() - t0) / 200 * 1000:.2f} ms/call (200 sequential)")

ms.execute_query(query="SELECT 1")
t0 = time.perf_counter()
for _ in range(200):
    ms.execute_query(query="SELECT 1")
print(f"execute_query('SELECT 1')  : {(time.perf_counter() - t0) / 200 * 1000:.2f} ms/call (200 sequential, includes the guard)")

big = "SELECT a.id FROM order_items a, order_items b, order_items c"
rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
c0 = ro_connections()
tracemalloc.start()
t0 = time.perf_counter()
out = ms.execute_query(query=big, max_rows=100)
dt = time.perf_counter() - t0
peak = tracemalloc.get_traced_memory()[1]
tracemalloc.stop()
rss1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
t1 = time.perf_counter()
ms.execute_query(query="SELECT 1")
nxt = (time.perf_counter() - t1) * 1000
print(f"729,000-row query, cap 100 : {dt:.3f} s, Python peak {peak / 1e6:.1f} MB, max RSS +{(rss1 - rss0) / 1e6:.1f} MB, "
      f"next call {nxt:.1f} ms, new connections {ro_connections() - c0}")

text = ms.execute_query(query="SELECT id, customer_id, status, total FROM orders", max_rows=40)
print(f"40-row result size         : {len(text)} chars")

c0 = ro_connections()
t0 = time.perf_counter()
for _ in range(50):
    ms.execute_query(query="SELECT id FROM order_items", max_rows=10)
print(f"50 truncated queries       : {(time.perf_counter() - t0) / 50 * 1000:.2f} ms/call, new connections {ro_connections() - c0}")
