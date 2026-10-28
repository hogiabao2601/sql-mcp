"""Per-engine facts the conformance suite needs: where the test database is, how to get ground truth,
and the SQL snippets that differ between engines. The suite's checks themselves are the same for all.
Select with SQL_MCP_ENGINE (default mysql) and, for a version matrix, SQL_MCP_CONFORMANCE_PORT."""
from __future__ import annotations

import json
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
ENGINE = os.environ.get("SQL_MCP_ENGINE", "mysql")


def _env_file(name: str) -> str | None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    return None


DEFAULT_PORTS = {
    "mysql": int(os.environ.get("SQL_MCP_MYSQL_PORT") or _env_file("SQL_MCP_MYSQL_PORT") or 33316),
    "mariadb": int(os.environ.get("SQL_MCP_MARIADB_PORT") or 33326),
    "postgres": int(os.environ.get("SQL_MCP_POSTGRES_PORT") or 54329),
    "sqlite": 0,
    "starrocks": int(os.environ.get("SQL_MCP_STARROCKS_PORT") or 39030),
}
PORT = int(os.environ.get("SQL_MCP_CONFORMANCE_PORT") or DEFAULT_PORTS[ENGINE])
HOST = "127.0.0.1"
RO = {"user": "readonly_user", "password_env": "SQL_MCP_TEST_RO_PASSWORD"}
RW = {"user": "app_writer", "password_env": "SQL_MCP_TEST_RW_PASSWORD"}
os.environ.setdefault("SQL_MCP_TEST_RO_PASSWORD", "readonly-pw-test-only")
os.environ.setdefault("SQL_MCP_TEST_RW_PASSWORD", "writer-pw-test-only")
WRONG_CA = ROOT / ".smoke" / "wrong-ca.pem"


class Spec:
    engine = "?"

    def session_cipher(self, profile: str) -> str:
        from sql_mcp.config import get_profile
        from sql_mcp.executor import run
        return run(get_profile(profile), self.cipher, mask=False)["rows"][0][1]
    has_server = True                         # False: in-process engine (nothing to kill, no network, no TLS)
    read_only_session = True                  # False: the engine has NO read-only mode (StarRocks)
    has_foreign_keys = True
    # (statement, a local file it must NOT create) tried WITHOUT the guard, through the writable "writer" profile
    unguarded_escapes: list[tuple[str, str]] = []
    system_namespace: str | None = "information_schema"
    cap_namespace = "information_schema"      # a namespace big enough to hit the schema:// cap

    def namespace_value(self, name: str) -> str:
        """What current_namespace returns while `name` is the call's database."""
        return name

    def cap_truth(self) -> int:
        return self.ro_sql("SELECT COUNT(DISTINCT table_name) FROM information_schema.columns "
                           "WHERE table_schema='information_schema'")[0][0]      # what this account may see

    def ro_sql(self, sql: str):
        """As readonly_user over a PLAIN connection (no sql-mcp, no read-only session): what the account itself
        is allowed to do and to see. That is layer 3 alone."""
        return self.admin_sql(sql, user="readonly_user", password="readonly-pw-test-only")
    mysql_family = False
    connection_database: str | None = None   # PostgreSQL: the database the profile connects to
    no_namespace_value: str | None = None     # current namespace when the call chooses no database/schema
    engine_refusals: list[str] = []
    limit_forms: list[tuple[str, list[int]]] = [("SELECT id FROM orders ORDER BY id LIMIT 3 OFFSET 2", [3, 4, 5])]
    heavy = ("SELECT COUNT(*) FROM order_items a, order_items b, order_items c, order_items d, order_items e "
             "WHERE (a.id + b.id + c.id + d.id + e.id) % 7 = 0")
    big = "SELECT a.id FROM order_items a, order_items b, order_items c"            # 729,000 rows
    repeat = "SELECT REPEAT('x', 400) AS t FROM order_items a, order_items b"
    long_cell = "SELECT REPEAT('y', 5000) AS t"
    common_refusals = ["DROP TABLE orders", "SELECT 1 /* x */", "SELECT 1; SELECT 2", "SELECT * FROM orders FOR UPDATE",
                       "DELETE FROM orders WHERE id = -1", "SELECT 1 INTO OUTFILE '/tmp/x'"]

    @property
    def tls_ca(self) -> pathlib.Path | None:
        """A CA that verifies the test server, or None when that is not testable here."""
        return None

    def profiles(self, base) -> dict:
        db = self.connection_database

        def ns(name):
            return {"database": db, "schema": name} if db else {"database": name}
        tls = {"tls_disable": dict(base, **RO, **ns("shop"), tls="disable"),
               "tls_require": dict(base, **RO, **ns("shop"), tls="require")}
        if self.tls_ca:
            tls["tls_verify_full"] = dict(base, **RO, **ns("shop"), tls="verify-full", tls_ca=str(self.tls_ca))
            tls["tls_wrong_ca"] = dict(base, **RO, **ns("shop"), tls="verify-ca", tls_ca=str(WRONG_CA))
        return {
            "shop": dict(base, **RO, **ns("shop"), pool_size=3),
            "analytics": dict(base, **RO, **ns("analytics"), mask_columns=["password", "token", "email"]),
            "writer": dict(base, **RW, **ns("shop")),
            "nodb": dict(base, **RO, **({"database": db} if db else {}), pool_size=1),
            "one": dict(base, **RO, **ns("shop"), pool_size=1),
            **tls,
        }


class MySQLFamily(Spec):
    """MySQL and MariaDB share the seed (mysql-init/), the protocol, and most SQL."""
    mysql_family = True
    session_id = "SELECT CONNECTION_ID()"
    current_namespace = "SELECT DATABASE()"
    concurrency = "SELECT CONNECTION_ID(), SLEEP(0.3)"
    slow_two_rows = "SELECT o.id FROM (SELECT SLEEP(1) AS s) x, shop.orders o"
    unguarded_slow = ("SHOW DATABASES WHERE (SELECT COUNT(*) FROM order_items a, order_items b, order_items c, "
                      "order_items d, order_items e WHERE (a.id + b.id + c.id + d.id + e.id) % 7 = 0) >= 0")
    yesterday = ("SELECT COUNT(*) AS n, ROUND(SUM(total),2) AS revenue FROM orders "
                 "WHERE DATE(created_at)=CURDATE()-INTERVAL 1 DAY")
    error_channel = "SELECT EXTRACTVALUE(1, CONCAT(0x7e, (SELECT api_token FROM app_users LIMIT 1))) AS x"
    cafe_query = "SELECT `crème` FROM `café`"
    cipher = "SHOW SESSION STATUS LIKE 'Ssl_cipher'"
    denied = "command denied"
    pk_index = "PRIMARY(id)"
    explain_expect = "PRIMARY"
    limit_forms = Spec.limit_forms + [("SELECT id FROM orders ORDER BY id LIMIT 2, 3", [3, 4, 5])]
    family_refusals = ["SELECT SLEEP(1)", "EXPLAIN ANALYZE SELECT 1", "DESC ANALYZE SELECT 1",
                       "SELECT /*+ MAX_EXECUTION_TIME(60000) */ 1", "SELECT `sleep`(1)", unguarded_slow]

    def admin_sql(self, sql: str, user: str = "root", password: str = "rootpw-test-only"):
        import pymysql
        c = pymysql.connect(host=HOST, port=PORT, user=user, password=password, database="shop", autocommit=True)
        try:
            with c.cursor() as cur:
                cur.execute(sql)
                return cur.fetchall() if cur.description else None
        finally:
            c.close()

    def version(self) -> str:
        return self.admin_sql("SELECT VERSION()")[0][0]

    def running_queries(self) -> list[int]:
        return [r[0] for r in self.admin_sql(
            "SELECT id FROM information_schema.processlist WHERE user='readonly_user' AND command='Query'")]

    def kill(self, sid) -> None:
        self.admin_sql(f"KILL {int(sid)}")

    def pin_check(self, run_one) -> bool:
        """Layer 1 and the engine must read text the same way (REVIEW #8): force the server default to the
        risky modes, then show that the server's own session has them removed."""
        old = self.admin_sql("SELECT @@GLOBAL.sql_mode")[0][0]
        try:
            self.admin_sql(f"SET GLOBAL sql_mode = 'NO_BACKSLASH_ESCAPES,ANSI_QUOTES,{old}'")
            mode = run_one("SELECT @@SESSION.sql_mode")
            return "NO_BACKSLASH_ESCAPES" not in mode and "ANSI_QUOTES" not in mode and "STRICT_TRANS_TABLES" in mode
        finally:
            self.admin_sql(f"SET GLOBAL sql_mode = '{old}'")


class MySQL(MySQLFamily):
    engine = "mysql"
    family_refusals = MySQLFamily.family_refusals + ["SELECT * FROM orders FOR SHARE"]

    def has_tls(self) -> bool:            # @@have_ssl was removed in MySQL 8.4
        return bool(self.admin_sql("SHOW GLOBAL STATUS LIKE 'Current_tls_cert'")[0][1])


class MariaDB(MySQLFamily):
    engine = "mariadb"
    engine_refusals = ["SET STATEMENT max_statement_time=0 FOR SELECT COUNT(*) FROM orders",   # overrides the limit
                       "SELECT * FROM orders LOCK IN SHARE MODE"]
    limit_forms = MySQLFamily.limit_forms + [
        ("SELECT id FROM orders ORDER BY id OFFSET 2 ROWS FETCH FIRST 3 ROWS ONLY", [3, 4, 5])]   # 10.6+

    def has_tls(self) -> bool:
        return str(self.admin_sql("SELECT @@have_ssl")[0][0]).upper() == "YES"


class Postgres(Spec):
    engine = "postgres"
    connection_database = "app"
    no_namespace_value = "public"
    session_id = "SELECT pg_backend_pid()"
    current_namespace = "SELECT current_schema()"
    concurrency = "SELECT pg_backend_pid(), pg_sleep(0.3)"
    slow_two_rows = "SELECT o.id FROM (SELECT pg_sleep(1)) x, shop.orders o"
    unguarded_slow = "SELECT pg_sleep(30)"
    yesterday = ("SELECT COUNT(*) AS n, ROUND(SUM(total),2) AS revenue FROM orders "
                 "WHERE created_at::date = CURRENT_DATE - 1")
    error_channel = "SELECT CAST((SELECT api_token FROM app_users LIMIT 1) AS integer) AS x"
    cafe_query = 'SELECT "crème" FROM "café"'
    cipher = "SELECT 'Ssl_cipher', COALESCE((SELECT cipher FROM pg_stat_ssl WHERE pid = pg_backend_pid()), '')"
    denied = "permission denied"
    pk_index = "orders_pkey(id)"
    explain_expect = "Scan"
    limit_forms = Spec.limit_forms + [
        ("SELECT id FROM orders ORDER BY id OFFSET 2 ROWS FETCH FIRST 3 ROWS ONLY", [3, 4, 5]),
        ("SELECT id FROM orders ORDER BY id OFFSET 2 LIMIT 3", [3, 4, 5])]
    family_refusals = ["SELECT pg_sleep(1)", "SELECT set_config('default_transaction_read_only', 'off', false)",
                       "WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d", "COPY orders TO STDOUT",
                       "SELECT * INTO new_t FROM orders", "EXPLAIN ANALYZE SELECT 1", "SET statement_timeout = 0",
                       "SELECT lo_import('/etc/passwd')", "SELECT * FROM orders FOR SHARE", "SHOW search_path"]

    @property
    def tls_ca(self):
        ca = ROOT / "docker" / "pg-tls" / "ca.crt"
        return ca if ca.exists() and self.has_tls() else None

    def admin_sql(self, sql: str, user: str = "postgres", password: str = "rootpw-test-only"):
        import psycopg
        with psycopg.connect(host=HOST, port=PORT, user=user, password=password, dbname="app", autocommit=True,
                             options="-c search_path=shop") as c:
            cur = c.execute(sql)
            return cur.fetchall() if cur.description else None

    def version(self) -> str:
        return self.admin_sql("SHOW server_version")[0][0]

    def running_queries(self) -> list[int]:
        return [r[0] for r in self.admin_sql(
            "SELECT pid FROM pg_stat_activity WHERE usename='readonly_user' AND state='active'")]

    def kill(self, sid) -> None:
        self.admin_sql(f"SELECT pg_terminate_backend({int(sid)})")

    def has_tls(self) -> bool:
        return self.admin_sql("SHOW ssl")[0][0] == "on"

    def pin_check(self, run_one) -> bool:
        """standard_conforming_strings must be on (a backslash is a plain character in '...'), whatever the default."""
        try:
            self.admin_sql("ALTER DATABASE app SET standard_conforming_strings = off")
            return run_one("SHOW standard_conforming_strings") == "on"
        finally:
            self.admin_sql("ALTER DATABASE app RESET standard_conforming_strings")


class SQLite(Spec):
    """No server: files opened in-process. Layer 3 is the FILE's permissions (the seed makes them 0444)."""
    engine = "sqlite"
    has_server = False
    system_namespace = None
    cap_namespace = "wide"
    session_id = "SELECT sql_mcp_connection_id()"
    current_namespace = ("SELECT replace(file, rtrim(file, replace(file, '/', '')), '') "
                         "FROM pragma_database_list WHERE name = 'main'")
    yesterday = ("SELECT COUNT(*) AS n, ROUND(SUM(total),2) AS revenue FROM orders "
                 "WHERE date(created_at) = date('now', '-1 day')")
    repeat = "SELECT substr(hex(zeroblob(200)), 1, 400) AS t FROM order_items a, order_items b"
    long_cell = "SELECT substr(hex(zeroblob(2500)), 1, 5000) AS t"
    error_channel = "SELECT json(api_token) AS x FROM app_users"          # fails while reading a masked column
    cafe_query = 'SELECT "crème" FROM "café"'
    cipher = None
    denied = "readonly database"
    pk_index = "PRIMARY(id)"
    explain_expect = "orders"
    no_namespace_value = "shop.db"
    limit_forms = Spec.limit_forms + [("SELECT id FROM orders ORDER BY id LIMIT 2, 3", [3, 4, 5])]
    family_refusals = ["ATTACH DATABASE '/tmp/x.db' AS x", "PRAGMA query_only = OFF", "SELECT load_extension('x')",
                       "VACUUM INTO '/tmp/sqlmcp-copy.db'", "SELECT writefile('/tmp/x', 'y')", "BEGIN"]
    dir = ROOT / ".smoke" / "sqlite"
    _calibrated: int | None = None

    def namespace_value(self, name: str) -> str:
        return f"{name}.db"

    def _loops(self) -> int:
        """Recursion depth that takes ~0.3 s here (SQLite has no sleep(); calibrated once per run)."""
        if self._calibrated is None:
            import sqlite3
            import time
            c = sqlite3.connect(":memory:")
            t0 = time.perf_counter()
            c.execute("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 1000000) "
                      "SELECT count(*) FROM c").fetchone()
            SQLite._calibrated = int(1_000_000 * 0.3 / (time.perf_counter() - t0))
        return self._calibrated

    def _busy(self, seconds: float) -> str:
        return (f"(SELECT count(*) FROM (WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c "
                f"WHERE x < {int(self._loops() * seconds / 0.3)}) SELECT x FROM c))")

    @property
    def concurrency(self):
        return f"SELECT sql_mcp_connection_id(), {self._busy(0.3)}"

    @property
    def slow_two_rows(self):
        return f"SELECT o.id FROM (SELECT {self._busy(1.0)} AS s) x, orders o"

    unguarded_slow = Spec.heavy
    _esc = ROOT / ".smoke"            # inside the project: even a broken safeguard can only write here
    unguarded_escapes = [
        (f"ATTACH DATABASE '{_esc}/escape-attach.db' AS x", f"{_esc}/escape-attach.db"),
        (f"VACUUM INTO '{_esc}/escape-vacuum.db'", f"{_esc}/escape-vacuum.db"),
        (f"SELECT writefile('{_esc}/escape-writefile.txt', 'x')", f"{_esc}/escape-writefile.txt"),
        ("PRAGMA query_only = OFF", ""), ("CREATE TEMP TABLE t (i)", ""), ("DETACH DATABASE analytics", ""),
    ]

    def profiles(self, base) -> dict:
        from tests.fixtures.sqlite_seed import build
        if not (self.dir / "shop.db").exists():
            build(self.dir)
        files = {n: str(self.dir / f"{n}.db") for n in ("shop", "analytics", "odd-names", "wide")}
        writer = dict(files, shop=str(self.dir / "shop-writable.db"))
        sq = {"engine": "sqlite"}
        return {
            "shop": dict(sq, databases=files, database="shop", pool_size=3),
            "analytics": dict(sq, databases=files, database="analytics", mask_columns=["password", "token", "email"]),
            "writer": dict(sq, databases=writer, database="shop"),
            "nodb": dict(sq, databases=files, pool_size=1),
            "one": dict(sq, databases=files, database="shop", pool_size=1),
        }

    def admin_sql(self, sql: str, path: str | None = None):
        import sqlite3
        c = sqlite3.connect(f"file:{path or self.dir / 'shop.db'}?mode=ro", uri=True)
        try:
            for n in ("shop", "analytics", "odd-names", "wide"):
                c.execute(f"ATTACH DATABASE 'file:{self.dir / (n + '.db')}?mode=ro' AS \"{n}\"")
            cur = c.execute(sql)
            return cur.fetchall() if cur.description else None
        finally:
            c.close()

    def ro_sql(self, sql: str):
        """A PLAIN read-write connection (no sql-mcp) to the 0444 file: the OS permission is layer 3 here."""
        import sqlite3
        c = sqlite3.connect(self.dir / "shop.db")
        try:
            return c.execute(sql).fetchall()
        finally:
            c.close()

    def version(self) -> str:
        import sqlite3
        return f"SQLite {sqlite3.sqlite_version} (in-process)"

    def cap_truth(self) -> int:
        return self.admin_sql("SELECT COUNT(*) FROM wide.sqlite_schema WHERE type = 'table'")[0][0]

    def running_queries(self) -> list[int]:
        return []                              # in-process: nothing can outlive the call

    def has_tls(self) -> bool:
        return False

    def pin_check(self, run_one) -> bool:
        return run_one("PRAGMA query_only") == 1


class StarRocks(MySQLFamily):
    """MySQL protocol, but NO read-only mode and no foreign keys. A SELECT-only user can still export files
    (INSERT INTO FILES / INTO OUTFILE): the suite proves both gaps instead of skipping them (README)."""
    engine = "starrocks"
    concurrency = "SELECT CONNECTION_ID(), SLEEP(1)"     # StarRocks' SLEEP takes whole seconds: SLEEP(0.3) = SLEEP(0)
    read_only_session = False
    has_foreign_keys = False
    no_namespace_value = ""
    denied = "Access denied"
    explain_expect = "orders"
    cipher = "SELECT 'Ssl_cipher', ''"                 # no TLS in this image (no ssl variables at all)
    error_channel = "SELECT raise_error(api_token) AS x FROM app_users"
    container = "sql-mcp-starrocks"
    export_dir = "/tmp/sqlmcp-conformance-gap/"
    engine_refusals = [
        "SELECT /*+ SET_VAR(query_timeout = 3600) */ 1", "SET query_timeout = 3600", "SET CATALOG default_catalog",
        f"INSERT INTO FILES('path' = 'file://{export_dir}', 'format' = 'csv') SELECT * FROM orders",
        f"SELECT * FROM orders INTO OUTFILE 'file://{export_dir}' FORMAT AS CSV",
        "SUBMIT TASK AS INSERT INTO orders SELECT * FROM orders", f"EXPORT TABLE orders TO 'file://{export_dir}'",
        "CACHE SELECT * FROM orders", "ADMIN SET FRONTEND CONFIG ('query_timeout' = '3600')",
        "SELECT * FROM FILES('path' = 's3://bucket/x/', 'format' = 'parquet', 'aws.s3.access_key' = 'k')"]

    def admin_sql(self, sql: str, user: str = "root", password: str = ""):
        self._seed()
        return MySQLFamily.admin_sql(self, sql, user, password)

    def _seed(self):
        import pymysql
        import re
        if getattr(StarRocks, "_seeded", False):
            return
        c = pymysql.connect(host=HOST, port=PORT, user="root", password="", autocommit=True)
        try:
            with c.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema IN "
                            "('shop', 'analytics', 'odd-names')")
                if cur.fetchone()[0] != 9:
                    text = (ROOT / "tests" / "fixtures" / "starrocks_seed.sql").read_text()
                    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
                    for stmt in (s.strip() for s in re.split(r";\s*\n", body)):
                        if stmt:
                            cur.execute(stmt)
        finally:
            c.close()
        StarRocks._seeded = True

    def profiles(self, base) -> dict:
        self._seed()
        return super().profiles(base)

    def version(self) -> str:
        return "StarRocks " + self.admin_sql("SELECT current_version()")[0][0]

    def running_queries(self) -> list[int]:
        return [r[1] for r in self.admin_sql("SHOW FULL PROCESSLIST") if r[2] == "readonly_user" and r[5] == "Query"]

    @property
    def tls_ca(self):
        ca = ROOT / "docker" / "sr-tls" / "ca.pem"      # written by docker/starrocks-tls.sh
        return ca if ca.exists() and self.has_tls() else None

    def has_tls(self) -> bool:
        """StarRocks has no TLS status variables: try a verified connection instead."""
        import pymysql
        ca = ROOT / "docker" / "sr-tls" / "ca.pem"
        if not ca.exists():
            return False
        try:
            pymysql.connect(host=HOST, port=PORT, user="root", password="", connect_timeout=5,
                            ssl={"ca": str(ca), "check_hostname": True}).close()
            return True
        except Exception:
            return False

    def session_cipher(self, profile: str) -> str:
        """No Ssl_cipher status in StarRocks: read it from the pooled connection's own socket."""
        from sql_mcp.config import get_profile
        from sql_mcp.executor import engine_for, run
        run(get_profile(profile), "SELECT 1")
        sock = engine_for(get_profile(profile))[1]._idle[-1].raw._sock
        return sock.cipher()[0] if hasattr(sock, "cipher") else ""

    def pin_check(self, run_one) -> bool:
        """StarRocks accepts NO_BACKSLASH_ESCAPES / ANSI_QUOTES but ignores them: a backslash always escapes,
        which is what the guard assumes. Check exactly that property."""
        return run_one("SELECT LENGTH('a\\\\b')") == 3

    def export_as_select_only_user(self) -> int:
        """The documented gap: a SELECT-only account writes a file. Returns the rows written; cleans up after."""
        import subprocess
        try:
            rows = self.ro_sql(f"INSERT INTO FILES('path' = 'file://{self.export_dir}', 'format' = 'csv') "
                               "SELECT id FROM orders")
            return 40 if rows is None else len(rows)
        finally:
            subprocess.run(["docker", "exec", self.container, "rm", "-rf", self.export_dir],
                           capture_output=True, env={**os.environ, "PATH": os.environ.get("PATH", "") +
                                                     ":" + os.path.expanduser("~/.docker/bin")})


SPEC = {"mysql": MySQL, "mariadb": MariaDB, "postgres": Postgres, "sqlite": SQLite, "starrocks": StarRocks}[ENGINE]()


def write_config() -> pathlib.Path:
    base = {"engine": ENGINE, "host": HOST, "port": PORT}
    path = ROOT / ".smoke" / f"conformance-{ENGINE}-{PORT}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"default": "shop", "profiles": SPEC.profiles(base)}))
    return path
