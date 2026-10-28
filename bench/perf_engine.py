"""Engine-generic performance check (same measurements as perf_mysql.py, no performance_schema needed).

    SQL_MCP_ENGINE=mariadb [SQL_MCP_CONFORMANCE_PORT=33327] .venv/bin/python bench/perf_engine.py
"""
import os
import pathlib
import sys
import time
import tracemalloc

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from tests.conformance.engines import ENGINE, PORT, SPEC, write_config  # noqa: E402

os.environ["SQL_MCP_CONFIG"] = str(write_config())
os.environ["SQL_MCP_CACHE_TTL"] = "0"
from sql_mcp import server as srv  # noqa: E402
from sql_mcp.config import get_profile  # noqa: E402
from sql_mcp.executor import run  # noqa: E402


def sid():
    return run(get_profile("one"), SPEC.session_id)["rows"][0][0]


print(f"[{ENGINE} {SPEC.version()} on {PORT}]")
srv.execute_query(query="SELECT 1", profile="one")
t0 = time.perf_counter()
for _ in range(200):
    srv.execute_query(query="SELECT 1", profile="one")
print(f"  execute_query('SELECT 1')  : {(time.perf_counter() - t0) / 200 * 1000:.2f} ms/call (200 sequential)")
s0 = sid()
tracemalloc.start()
t0 = time.perf_counter()
srv.execute_query(query=SPEC.big, max_rows=100, profile="one")
dt = time.perf_counter() - t0
peak = tracemalloc.get_traced_memory()[1]
tracemalloc.stop()
print(f"  729,000-row query, cap 100 : {dt:.3f} s, Python peak {peak / 1e6:.1f} MB, same session after: {sid() == s0}")
print(f"  40-row result size         : {len(srv.execute_query(query='SELECT id, customer_id, status, total FROM orders', max_rows=40))} chars")
s0 = sid()
t0 = time.perf_counter()
for _ in range(50):
    srv.execute_query(query="SELECT id FROM order_items", max_rows=10, profile="one")
print(f"  50 truncated queries       : {(time.perf_counter() - t0) / 50 * 1000:.2f} ms/call, same session after: {sid() == s0}")
