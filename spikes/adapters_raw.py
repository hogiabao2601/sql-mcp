"""Option B spike: raw drivers behind our own small Dialect classes (pymysql, psycopg 3, sqlite3)."""
from __future__ import annotations

import sqlite3
import threading
import time
from collections import deque

import psycopg
import pymysql
import sqlglot
from pymysql.cursors import SSCursor

READ_PRAGMAS = {"table_info", "table_xinfo", "foreign_key_list", "index_list", "index_info", "index_xinfo",
                "table_list", "database_list", "collation_list"}   # all read-only, whatever the argument


def sqlite_authorizer(deny):
    """Deny writes and ATTACH; allow only pragmas that never change state (arg2 is the pragma argument)."""
    def auth(action, arg1, arg2, _db, _trigger):
        if action == sqlite3.SQLITE_PRAGMA:
            return sqlite3.SQLITE_OK if arg1 in READ_PRAGMAS else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY if action in deny else sqlite3.SQLITE_OK
    return auth


class Pool:
    """Capacity is a Condition, so release AND discard both wake a waiter (review finding 1)."""
    def __init__(self, open_fn, size: int, max_age_s: float = 1800):
        self.open_fn, self.size, self.max_age = open_fn, size, max_age_s
        self.idle: deque = deque()
        self.created = 0
        self.cv = threading.Condition()

    def acquire(self, timeout: float = 10):
        with self.cv:
            end = time.monotonic() + timeout
            while True:
                while self.idle:
                    conn, born = self.idle.pop()
                    if time.monotonic() - born < self.max_age:
                        return conn, born
                    self.created -= 1
                    _close(conn)
                if self.created < self.size:
                    self.created += 1
                    break
                if not self.cv.wait(end - time.monotonic()):
                    raise TimeoutError("pool exhausted")
        try:
            return self.open_fn(), time.monotonic()
        except Exception:
            with self.cv:
                self.created -= 1
                self.cv.notify()
            raise

    def release(self, conn, born):
        with self.cv:
            self.idle.append((conn, born))
            self.cv.notify()

    def discard(self, conn):
        _close(conn)
        with self.cv:
            self.created -= 1
            self.cv.notify()


def _close(conn):
    try:
        conn.close()
    except Exception:
        pass


class Dialect:
    name = "?"
    sqlglot = "?"
    stream_close = "drain"          # what to do with unread rows: "drain" or "discard"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.timeout_ms = cfg.get("timeout_ms", 5000)
        self.pool = Pool(self.open, cfg.get("pool_size", 3))

    def limit(self, sql: str, n: int) -> str:
        """Push the row cap into the statement, so the server stops producing rows (no drain, no discard)."""
        tree = sqlglot.parse_one(sql, read=self.sqlglot)
        old = tree.args.get("limit")
        cur = int(old.expression.name) if old is not None and old.expression.is_int else None
        if cur is None or cur > n:
            tree = tree.limit(n, copy=False) if hasattr(tree, "limit") else tree
        return tree.sql(dialect=self.sqlglot)

    def query(self, sql: str, max_rows: int = 100, inject_limit: bool = False) -> dict:
        if inject_limit:
            sql = self.limit(sql, max_rows + 1)
        conn, born = self.pool.acquire()
        ok = False
        try:
            cur = self.begin(conn)
            self.execute(cur, sql)
            cols = [d[0] for d in cur.description]
            rows, truncated = [], None
            while True:
                batch = cur.fetchmany(200)
                if not batch:
                    break
                take = batch[: max_rows - len(rows)]
                rows.extend(list(r) for r in take)
                if len(take) < len(batch) or len(rows) >= max_rows and cur.fetchmany(1):
                    truncated = f"row limit {max_rows}"
                    break
            ok = self.finish(conn, cur, truncated is not None)
            return {"columns": cols, "rows": rows, "truncated": truncated}
        finally:
            (self.pool.release(conn, born) if ok else self.pool.discard(conn))

    # per-dialect hooks
    def open(self): raise NotImplementedError
    def begin(self, conn): return conn.cursor()
    def execute(self, cur, sql): cur.execute(sql)
    def finish(self, conn, cur, early: bool) -> bool:
        cur.close()
        return True


class MySQL(Dialect):
    name, sqlglot = "mysql", "mysql"
    session = "SET SESSION transaction_read_only=1, SESSION max_execution_time={ms}"

    def open(self):
        c = self.cfg
        return pymysql.connect(host=c["host"], port=c["port"], user=c["user"], password=c["password"],
                               database=c.get("database"), autocommit=True, charset="utf8mb4",
                               init_command=self.session.format(ms=self.timeout_ms, s=self.timeout_ms / 1000))

    def begin(self, conn):
        return conn.cursor(SSCursor)

    def finish(self, conn, cur, early):
        if early and self.stream_close == "discard":
            return False
        cur.close()                 # drains unread rows
        return True

    def tables(self, schema):
        cur = self.pool.acquire()
        conn, born = cur
        try:
            with conn.cursor() as k:
                k.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s ORDER BY 1", (schema,))
                return [r[0] for r in k.fetchall()]
        finally:
            self.pool.release(conn, born)

    def describe(self, schema, table):
        conn, born = self.pool.acquire()
        try:
            with conn.cursor() as k:
                k.execute("SELECT column_name, column_type, is_nullable FROM information_schema.columns "
                          "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position", (schema, table))
                cols = k.fetchall()
                k.execute("SELECT column_name, referenced_table_name, referenced_column_name FROM "
                          "information_schema.key_column_usage WHERE table_schema=%s AND table_name=%s "
                          "AND referenced_table_name IS NOT NULL", (schema, table))
                out_fk = k.fetchall()
                k.execute("SELECT table_name, column_name FROM information_schema.key_column_usage "
                          "WHERE referenced_table_schema=%s AND referenced_table_name=%s", (schema, table))
                in_fk = k.fetchall()
            return {"columns": [list(c) for c in cols], "fk_out": [list(f) for f in out_fk], "fk_in": [list(f) for f in in_fk]}
        finally:
            self.pool.release(conn, born)


class MariaDB(MySQL):
    name = "mariadb"
    session = "SET SESSION transaction_read_only=1, SESSION max_statement_time={s}"


class Postgres(Dialect):
    name, sqlglot = "postgres", "postgres"

    def open(self):
        c = self.cfg
        conn = psycopg.connect(host=c["host"], port=c["port"], user=c["user"], password=c["password"],
                               dbname=c["database"], options=f"-c statement_timeout={self.timeout_ms}")
        conn.read_only = True       # every transaction starts READ ONLY; can't be switched after the first query
        return conn

    def begin(self, conn):
        return conn.cursor(name="mcp")     # server-side cursor: rows come in fetchmany batches

    def finish(self, conn, cur, early):
        cur.close()                 # CLOSE on the server, no drain
        conn.rollback()             # ends the read-only transaction; also undoes any set_config(..., false)
        return True

    def tables(self, schema):
        conn, born = self.pool.acquire()
        try:
            rows = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s ORDER BY 1",
                                (schema,)).fetchall()
            conn.rollback()
            return [r[0] for r in rows]
        finally:
            self.pool.release(conn, born)

    def describe(self, schema, table):
        conn, born = self.pool.acquire()
        try:
            cols = conn.execute("SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                                "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position", (schema, table)).fetchall()
            fks = conn.execute(
                "SELECT a.attname, cf.relname, af.attname, c.relname FROM pg_constraint k "
                "JOIN pg_class c ON c.oid = k.conrelid JOIN pg_class cf ON cf.oid = k.confrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN LATERAL unnest(k.conkey, k.confkey) AS u(a, b) ON true "
                "JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = u.a "
                "JOIN pg_attribute af ON af.attrelid = k.confrelid AND af.attnum = u.b "
                "WHERE k.contype = 'f' AND n.nspname = %s AND (c.relname = %s OR cf.relname = %s)",
                (schema, table, table)).fetchall()
            conn.rollback()
            return {"columns": [list(c) for c in cols],
                    "fk_out": [[a, t, b] for a, t, b, src in fks if src == table],
                    "fk_in": [[src, a] for a, t, b, src in fks if t == table]}
        finally:
            self.pool.release(conn, born)


class SQLite(Dialect):
    name, sqlglot = "sqlite", "sqlite"
    DENY = {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE, sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_PRAGMA}

    def open(self):
        conn = sqlite3.connect(f"file:{self.cfg['path']}?mode=ro", uri=True, check_same_thread=False)
        conn.execute("PRAGMA query_only = ON")
        conn.set_authorizer(sqlite_authorizer(self.DENY))
        return conn

    def execute(self, cur, sql):
        deadline = time.monotonic() + self.timeout_ms / 1000
        cur.connection.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        cur.execute(sql)

    def tables(self, schema):
        r = self.query("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY 1", 1000)
        return [x[0] for x in r["rows"]]

    def describe(self, schema, table):
        q = table.replace('"', '""')
        cols = self.query(f"SELECT name, type, \"notnull\" FROM pragma_table_info('{q}')", 1000)["rows"]
        fk = self.query(f"SELECT \"from\", \"table\", \"to\" FROM pragma_foreign_key_list('{q}')", 1000)["rows"]
        return {"columns": cols, "fk_out": fk, "fk_in": None}


DIALECTS = {"mysql": MySQL, "mariadb": MariaDB, "postgres": Postgres, "sqlite": SQLite}
