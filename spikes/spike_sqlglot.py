"""Spike: sqlglot as the layer-1 guard and as the basis for masking by SOURCE column.

    spikes/.venv/bin/python spikes/spike_sqlglot.py

Offline. Prints, per case: expected vs got, and a summary of mismatches.
"""
import importlib.util
import pathlib
import sys
import time

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

ROOT = pathlib.Path(__file__).resolve().parent.parent

DENY_FUNCS = {
    "mysql": {"sleep", "benchmark", "get_lock", "release_lock", "release_all_locks", "is_free_lock", "is_used_lock",
              "load_file", "master_pos_wait", "source_pos_wait", "wait_for_executed_gtid_set"},
    "postgres": {"pg_sleep", "pg_sleep_for", "pg_sleep_until", "set_config", "pg_advisory_lock", "pg_advisory_xact_lock",
                 "pg_try_advisory_lock", "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "lo_import", "lo_export",
                 "dblink", "dblink_exec", "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf"},
    "sqlite": {"load_extension", "readfile", "writefile"},
}
DENY_FUNCS["mariadb"] = DENY_FUNCS["mysql"]
WRITE_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter, exp.Command,
               exp.Into, exp.Lock, exp.Set, exp.Transaction, exp.Commit, exp.Rollback, exp.Use, exp.Copy,
               exp.Pragma, exp.Describe, exp.Cache, exp.Uncache, exp.LoadData, exp.Grant)
WRITE_NODES = tuple(n for n in WRITE_NODES if isinstance(n, type))


def func_name(f: exp.Func) -> str:
    return (f.name if isinstance(f, exp.Anonymous) else f.sql_name()).lower()


def classify(sql: str, dialect: str) -> str:
    """Return 'ok' or 'reject: <reason>'. Layer 1 only: a convenience, never the security boundary."""
    if dialect in ("mysql", "mariadb") and "/*!" in sql:
        return "reject: MySQL executable comment"           # the parser ignores comments; MySQL executes these
    try:
        stmts = [s for s in sqlglot.parse(sql, read={"mariadb": "mysql"}.get(dialect, dialect)) if s is not None]
    except SqlglotError as e:
        return f"reject: parse error ({str(e).splitlines()[0][:60]})"
    if len(stmts) != 1:
        return f"reject: {len(stmts)} statements"
    root = stmts[0]
    if not isinstance(root, (exp.Select, exp.SetOperation, exp.Subquery)):
        return f"reject: root is {type(root).__name__}"
    for node in root.walk():
        if isinstance(node, WRITE_NODES):
            return f"reject: contains {type(node).__name__}"
        if isinstance(node, exp.Func) and func_name(node) in DENY_FUNCS.get(dialect, set()):
            return f"reject: function {func_name(node)}()"
    return "ok"


# ---------------------------------------------------------------- cases
_spec = importlib.util.spec_from_file_location("tg", ROOT / "tests" / "test_guard.py")
sys.path.insert(0, str(ROOT))
OK, NO = "ok", "reject"
CASES = []   # (dialect, sql, expected, note)
tg_allowed = ["SELECT 1", "  select * from orders;  ", "WITH x AS (SELECT 1 AS a) SELECT * FROM x",
              "SELECT 'a;b' AS semicolon_in_string", "SELECT '#tag' AS hash_in_string",
              "SELECT * FROM t WHERE note LIKE '%into update delete%'",
              "SELECT update_time, create_time FROM information_schema.tables",
              "SELECT REPLACE(name, 'a', 'b') FROM t", "SELECT `delete` FROM t"]
for q in tg_allowed:
    CASES.append(("mysql", q, OK, "test_guard allowed"))
# test_guard allowed cases that are NOT a single SELECT: SHOW/DESCRIBE/EXPLAIN go to dedicated tools in the design
for q in ["SHOW TABLES", "SHOW CREATE TABLE orders", "DESCRIBE orders", "EXPLAIN SELECT * FROM orders"]:
    CASES.append(("mysql", q, NO, "test_guard allowed; design: not via execute_query"))
tg_blocked = ["DROP TABLE orders", "DELETE FROM orders", "UPDATE t SET a=1", "INSERT INTO t VALUES (1)",
              "SET GLOBAL read_only=0", "CALL do_things()", "LOAD DATA INFILE 'x' INTO TABLE t",
              "SELECT 1; DELETE FROM t", "SELECT 1;DELETE FROM t", "SELECT 1 /*!INTO OUTFILE '/tmp/x'*/",
              "SELECT 1--1 INTO OUTFILE '/tmp/x'", "SELECT * INTO OUTFILE '/tmp/x' FROM t",
              "SELECT 1 INTO/**/OUTFILE '/tmp/x'", "SELECT 1 INTO DUMPFILE '/tmp/x'", "SELECT SLEEP(5)",
              "SELECT BENCHMARK(1000000, MD5('a'))", "SELECT GET_LOCK('a', 1)", "SELECT LOAD_FILE('/etc/passwd')",
              "EXPLAIN ANALYZE SELECT 1", "SELECT * FROM t FOR UPDATE", "WITH x AS (SELECT 1) DELETE FROM t"]
for q in tg_blocked:
    CASES.append(("mysql", q, NO, "test_guard blocked"))
CASES.append(("mysql", "", NO, "test_guard blocked: empty"))
CASES.append(("mysql", "   ", NO, "test_guard blocked: blank"))
# review #11: regex false positives -> should now be allowed
for q in ["SELECT load FROM servers", "SELECT c.amount AS `call` FROM t c", "SELECT h.* FROM handler h",
          "SELECT 1--1", "(SELECT 1) UNION (SELECT 2)", "SELECT * FROM t WHERE kill_switch = 0"]:
    CASES.append(("mysql", q, OK, "review #11 false positive"))
# review #10 / #8: regex bypasses -> should now be rejected
for q in ["SELECT `sleep`(3)", "DESC ANALYZE SELECT 1", "SELECT id FROM t FOR SHARE", "SELECT id FROM t LOCK IN SHARE MODE",
          "SELECT SOURCE_POS_WAIT('f', 4, 10)", "SHOW DATABASES WHERE (SELECT COUNT(*) FROM a, b) > 0",
          "SELECT 'a\\', 1 INTO OUTFILE '/tmp/x'"]:
    CASES.append(("mysql", q, NO, "review #8/#10 bypass"))
# PostgreSQL
for q, e, n in [("SELECT 1", OK, ""), ("WITH x AS (SELECT 1 AS a) SELECT a FROM x", OK, ""),
                ("SELECT \"order-archive\".id FROM shop.\"order-archive\"", OK, "quoted name"),
                ("SELECT $$it's$$ AS dollar_quoted", OK, "dollar quoting"),
                ("SELECT 'a\\' AS standard_string", OK, "standard_conforming_strings: backslash is literal"),
                ("SELECT E'a\\'b' AS escape_string", OK, "E'' escape string"),
                ("WITH d AS (DELETE FROM shop.orders RETURNING *) SELECT * FROM d", NO, "data-modifying CTE"),
                ("SELECT set_config('default_transaction_read_only', 'off', false)", NO, "turns layer 2 off"),
                ("SELECT pg_sleep(10)", NO, ""), ("SELECT * FROM shop.orders FOR UPDATE", NO, ""),
                ("SELECT * FROM shop.orders FOR KEY SHARE", NO, ""), ("COPY shop.orders TO '/tmp/x'", NO, ""),
                ("SELECT lo_import('/etc/passwd')", NO, ""), ("SELECT * INTO new_t FROM shop.orders", NO, "SELECT INTO creates a table"),
                ("SET default_transaction_read_only = off", NO, ""), ("BEGIN READ WRITE", NO, ""),
                ("SELECT nextval('s')", OK, "parser allows; layer 2 refuses nextval in a read-only txn"),
                ("SELECT 1; SELECT 2", NO, ""), ("DO $$BEGIN DELETE FROM t; END$$", NO, ""),
                ("SELECT * FROM generate_series(1, 3) AS g(i) WHERE i > 1 ORDER BY 1 LIMIT 2", OK, "")]:
    CASES.append(("postgres", q, e, n))
# SQLite
for q, e, n in [("SELECT 1", OK, ""), ("SELECT * FROM \"order-archive\"", OK, ""),
                ("ATTACH DATABASE '/tmp/x.db' AS x", NO, "creates a file"), ("PRAGMA query_only = OFF", NO, ""),
                ("SELECT load_extension('x')", NO, ""), ("VACUUM INTO '/tmp/copy.db'", NO, "writes a file"),
                ("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT x FROM c LIMIT 5", OK, "")]:
    CASES.append(("sqlite", q, e, n))
# Dialects for later engines: just check that ordinary reads parse
for d, q in [("tsql", "SELECT TOP 5 [order id] FROM dbo.[order items] WITH (NOLOCK)"),
             ("tsql", "SELECT * INTO #tmp FROM t"),
             ("oracle", "SELECT * FROM shop.orders WHERE ROWNUM <= 5"),
             ("oracle", "SELECT * FROM shop.orders FETCH FIRST 5 ROWS ONLY"),
             ("mariadb", "SELECT JSON_VALUE(doc, '$.a') FROM t")]:
    CASES.append((d, q, NO if "INTO" in q else OK, "other dialects"))


def run_cases():
    mismatches = []
    print(f"{'dialect':<9} {'expect':<7} {'got':<52} sql")
    for d, q, e, n in CASES:
        got = classify(q, d)
        good = got.startswith(e)
        if not good:
            mismatches.append((d, q, e, got, n))
        print(f"{d:<9} {e:<7} {('' if good else '!! ') + got:<52.52} {q[:70]}")
    print(f"\n{len(CASES)} cases, {len(mismatches)} mismatches")
    for m in mismatches:
        print("  MISMATCH", m)


# ---------------------------------------------------------------- masking by source column
SCHEMA = {"analytics": {"app_users": {"id": "int", "email": "text", "password_hash": "text", "api_token": "text",
                                      "created_at": "datetime"},
                        "page_views": {"id": "int", "user_id": "int", "path": "text", "viewed_at": "datetime"}}}
MASK = ("password", "token", "email")


def is_masked(col: str) -> bool:
    return any(m in col.lower() for m in MASK)


def mask_plan(sql: str, dialect: str = "mysql", db: str = "analytics"):
    """Return ('ok', [output columns to mask]) or ('refuse', reason).
    Policy: a masked source column may appear ONLY as a bare column in the outermost SELECT list
    (it is then masked in the output). Any other use (expression, WHERE, JOIN, UNION branch,
    derived table, function argument) is refused, because its value could leak through the
    result, an error message, or a boolean oracle."""
    tree = sqlglot.parse_one(sql, read=dialect)
    try:
        tree = qualify(tree, schema=SCHEMA, db=db, dialect=dialect, validate_qualify_columns=True)
    except OptimizeError as e:
        return "refuse", f"cannot resolve columns ({e})"
    outer = tree if isinstance(tree, exp.Select) else None
    to_mask = []
    for scope in traverse_scope(tree):
        for col in scope.columns:
            src = scope.sources.get(col.table)
            if not isinstance(src, exp.Table) or not is_masked(col.name):
                continue
            proj = col.parent
            bare_top = (scope.expression is outer and
                        (proj is outer or (isinstance(proj, exp.Alias) and proj.parent is outer)))
            if not bare_top:
                return "refuse", f"{src.name}.{col.name} used in an expression/clause"
            to_mask.append((proj.alias_or_name))
    return "ok", to_mask


MASK_CASES = [
    "SELECT id, api_token FROM app_users",
    "SELECT api_token AS x FROM app_users",
    "SELECT * FROM app_users",
    "SELECT CONCAT(api_token, '') AS x FROM app_users",
    "SELECT 'n/a' AS x UNION ALL SELECT api_token FROM app_users",
    "SELECT t.x FROM (SELECT api_token AS x FROM app_users) t",
    "SELECT EXTRACTVALUE(1, CONCAT(0x7e, (SELECT api_token FROM app_users LIMIT 1))) AS x",
    "SELECT id FROM app_users WHERE api_token LIKE 'tok_a%'",
    "SELECT u.id, COUNT(*) FROM app_users u JOIN page_views v ON v.user_id = u.id GROUP BY u.id",
    "SELECT id, created_at FROM app_users",
]


def run_mask():
    print("\n== masking by source column (policy: masked columns only as bare top-level output)")
    for q in MASK_CASES:
        print(f"  {str(mask_plan(q)):<78.78} | {q[:80]}")


def run_perf():
    q = ("SELECT o.status, COUNT(*) AS n, ROUND(SUM(oi.quantity * oi.unit_price), 2) AS revenue FROM orders o "
         "JOIN order_items oi ON oi.order_id = o.id WHERE o.created_at >= NOW() - INTERVAL 7 DAY "
         "GROUP BY o.status ORDER BY revenue DESC")
    for name, fn in [("classify", lambda: classify(q, "mysql")),
                     ("mask_plan (qualify)", lambda: mask_plan("SELECT u.id, COUNT(*) FROM app_users u JOIN page_views v "
                                                               "ON v.user_id = u.id GROUP BY u.id"))]:
        fn()
        n, t0 = 300, time.perf_counter()
        for _ in range(n):
            fn()
        print(f"perf: {name}: {(time.perf_counter() - t0) / n * 1000:.2f} ms per query")


if __name__ == "__main__":
    print(f"sqlglot {sqlglot.__version__}")
    run_cases()
    run_mask()
    run_perf()
