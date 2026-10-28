"""Measure: truncated query (connection discarded) vs non-truncated, through a given port."""
import importlib.util, json, os, pathlib, sys, time
port = int(sys.argv[1]); n = int(sys.argv[2]) if len(sys.argv) > 2 else 50
cfg = pathlib.Path(__file__).parent / f"rc-{port}.json"
cfg.write_text(json.dumps({"profiles": {"p": {"port": port, "user": "readonly_user", "password": "readonly-pw-test-only", "database": "shop"}}}))
os.environ["MYSQL_CONFIG"] = str(cfg)
spec = importlib.util.spec_from_file_location("ms", pathlib.Path(__file__).parent.parent / "mysql_server.py")
ms = importlib.util.module_from_spec(spec); spec.loader.exec_module(ms)
p = ms.get_profile("p")
for label, sql in [("SELECT 1 (pooled)        ", "SELECT 1"),
                   ("LIMIT 10, not truncated  ", "SELECT id FROM order_items LIMIT 10"),
                   ("max_rows=10, truncated   ", "SELECT id FROM order_items")]:
    ms.run(p, "SELECT 1")
    t0 = time.perf_counter()
    for _ in range(n):
        ms.run(p, sql, max_rows=10)
    print(f"port {port}: {label} {(time.perf_counter() - t0) / n * 1000:7.2f} ms/call")
