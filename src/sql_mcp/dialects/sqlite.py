"""SQLite through the standard library. No server and NO ACCOUNTS: layer 3 is the files' own permissions
(make them read-only for the server's OS user). Layer 2, applied to every connection:

  * every file is opened with a mode=ro URI (the main file and each attached one), plus PRAGMA query_only=ON;
  * an AUTHORIZER that allows only SELECT, reading columns, recursive CTEs, read-only pragmas and functions
    that are not denied. Everything else (writes, DDL, ATTACH, DETACH, VACUUM, transactions, pragma changes)
    is refused by SQLite before the statement runs. It is installed after our own ATTACH statements;
  * a progress handler that aborts a statement at the time limit (SQLite has no statement timeout).

A profile maps names to files; each name is a "database" in the tools. The call's database becomes the main
file (so unqualified names resolve there) and every configured file is also attached under its own name,
so `shop.orders JOIN analytics.app_users` works.
"""
from __future__ import annotations

import pathlib
import sqlite3
import time
import urllib.parse

from ..guard import DENY_FUNCS
from ..pool import Conn
from .base import Dialect, ErrorKind, Rows

# Pragmas that never change anything, whatever their argument (the argument is a table/index name).
_PRAGMAS_ANY_ARG = {"table_info", "table_xinfo", "index_list", "index_info", "index_xinfo", "foreign_key_list",
                    "table_list", "database_list", "collation_list", "function_list", "pragma_list"}
# Pragmas that are reads only when called WITHOUT an argument (with one they set something).
_PRAGMAS_NO_ARG = {"query_only", "foreign_keys", "user_version", "schema_version", "application_id", "encoding",
                   "page_size", "page_count", "freelist_count", "journal_mode", "compile_options"}
_DENIED_FUNCTIONS = {f.lower() for f in DENY_FUNCS["sqlite"]}


def _authorizer(action, arg1, arg2, _db, _trigger):
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION:        # arg2 is the function name
        return sqlite3.SQLITE_DENY if (arg2 or "").lower() in _DENIED_FUNCTIONS else sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA:          # arg1 is the pragma, arg2 its argument
        name = (arg1 or "").lower()
        if name in _PRAGMAS_ANY_ARG or (name in _PRAGMAS_NO_ARG and arg2 is None):
            return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY                   # an allowlist: anything not named above is refused


def _uri(path: str) -> str:
    return "file:" + urllib.parse.quote(str(pathlib.Path(path).expanduser().resolve())) + "?mode=ro"


class SQLite(Dialect):
    name, label, sqlglot = "sqlite", "SQLite", "sqlite"
    system_schemas = ("temp",)

    def __init__(self, profile):
        super().__init__(profile)
        self.files = dict(profile.databases)

    # connections ------------------------------------------------------------------------------
    def _open_raw(self, main: str) -> sqlite3.Connection:
        # isolation_level=None: no implicit BEGIN, on every Python (autocommit= exists only from 3.12)
        raw = sqlite3.connect(_uri(self.files[main]), uri=True, check_same_thread=False, isolation_level=None,
                              timeout=5)
        try:
            for name, path in self.files.items():
                if name == "main":
                    continue      # the file is already open as the connection's implicit `main` database
                raw.execute(f"ATTACH DATABASE ? AS {self.quote(name)}", (_uri(path),))
            raw.execute("PRAGMA query_only = ON")
            ident = id(raw)
            raw.create_function("sql_mcp_connection_id", 0, lambda: ident, deterministic=True)
            raw.set_authorizer(_authorizer)          # last: from here on, ATTACH/PRAGMA changes are refused
        except Exception:
            raw.close()
            raise
        return raw

    def default_namespace(self) -> str:
        return self.p.database or next(iter(self.files))

    def open(self) -> Conn:
        main = self.default_namespace()
        return Conn(self._open_raw(main), db=main)

    def ping(self, raw) -> None:
        raw.execute("SELECT 1").fetchone()

    def use_database(self, conn: Conn, db: str | None) -> bool:
        """The main file decides what unqualified names mean. Opening a file is cheap, so switching = reopening."""
        target = db or self.default_namespace()
        if target not in self.files:
            raise sqlite3.OperationalError(f"unknown database {target!r}; configured: {', '.join(self.files)}")
        if conn.db != target:
            conn.raw.close()
            conn.raw = self._open_raw(target)
            conn.db = target
        return True

    # statements -------------------------------------------------------------------------------
    def execute(self, conn: Conn, sql: str, args, stream: bool):
        deadline = time.monotonic() + self.p.timeout_ms / 1000
        conn.raw.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        return conn.raw.execute(sql, args or ())     # rows are produced as they are fetched

    def finish(self, conn: Conn, cursor, complete: bool) -> bool:
        cursor.close()
        return True

    def error_kind(self, exc: BaseException) -> ErrorKind | None:
        if not isinstance(exc, sqlite3.Error):
            return None
        msg = str(exc).lower()
        if "interrupted" in msg:
            return ErrorKind.TIMEOUT
        if "not authorized" in msg or "readonly database" in msg or "authorization denied" in msg:
            return ErrorKind.READ_ONLY
        return ErrorKind.OTHER

    def cancel(self, conn: Conn) -> None:
        conn.raw.interrupt()

    def explain_prefix(self) -> str:
        return "EXPLAIN QUERY PLAN "          # plain EXPLAIN shows virtual-machine bytecode

    # names ------------------------------------------------------------------------------------
    def quote(self, name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def find_database(self, rows: Rows, name: str) -> str | None:
        return name if name in self.files else None

    def find_table(self, rows: Rows, db: str, name: str) -> str | None:
        r = rows("SELECT name FROM sqlite_schema WHERE type IN ('table', 'view') AND name = ?", (name,), db)
        return r[0][0] if r else None

    # introspection (the call's database is the main file) --------------------------------------
    def databases(self, rows: Rows) -> list[str]:
        return list(self.files)

    def tables_sql(self) -> str:
        # SQLite keeps no row estimates or sizes without ANALYZE / dbstat: those columns are NULL.
        return ("SELECT name, type, NULL AS engine, NULL AS est_rows, NULL AS data_mb, NULL AS index_mb, "
                "NULL AS comment FROM sqlite_schema WHERE type IN ('table', 'view') "
                "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' AND ? IS NOT NULL ORDER BY name")

    def describe(self, rows: Rows, db: str, table: str) -> dict:
        cols = rows('SELECT name, type, "notnull", dflt_value, pk FROM pragma_table_info(?) ORDER BY cid', (table,), db)
        if not cols:
            return {}
        idx = rows('SELECT il.name, il."unique", il.origin, ii.name FROM pragma_index_list(?) il '
                   "JOIN pragma_index_info(il.name) ii ORDER BY il.name, ii.seqno", (table,), db)
        out_fk = rows('SELECT "from", "table", "to" FROM pragma_foreign_key_list(?) ORDER BY id, seq', (table,), db)
        in_fk = rows('SELECT m.name, f."from", f."to" FROM sqlite_schema m JOIN pragma_foreign_key_list(m.name) f '
                     "WHERE m.type = 'table' AND f.\"table\" = ? ORDER BY m.name, f.id, f.seq", (table,), db)

        def col(c):
            name, ctype, notnull, default, pk = c
            parts = [name, ctype or "ANY"]
            if notnull:
                parts.append("NOT NULL")
            if pk:
                parts.append("PRI")
            if default is not None:
                parts.append(f"DEFAULT {default}")
            return " ".join(parts)

        indexes: dict[str, list] = {}
        for name, unique, origin, column in idx:
            indexes.setdefault("PRIMARY" if origin == "pk" else name, [bool(unique), []])[1].append(column)
        pk_cols = [c[0] for c in sorted((c for c in cols if c[4]), key=lambda c: c[4])]
        if pk_cols and "PRIMARY" not in indexes:     # INTEGER PRIMARY KEY is the rowid: no separate index
            indexes = {"PRIMARY": [True, pk_cols], **indexes}
        return {
            "table": f"{db}.{table}",
            "columns": [col(c) for c in cols],
            "indexes": [f"{n}({','.join(cs)}){' UNIQUE' if u else ''}" for n, (u, cs) in indexes.items()],
            "references": [f"{f} -> {db}.{t}.{to}" for f, t, to in out_fk],
            "referenced_by": [f"{db}.{t}.{f} -> {to}" for t, f, to in in_fk],
        }

    def search(self, rows: Rows, like: str, db: str | None) -> dict:
        tables, columns = [], []
        for name in ([db] if db else list(self.files)):
            tables += [f"{name}.{t}" for (t,) in rows(
                "SELECT name FROM sqlite_schema WHERE type IN ('table', 'view') AND name LIKE ? ESCAPE '\\' "
                "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name", (like,), name)]
            columns += [f"{name}.{t}.{c} {ty}" for t, c, ty in rows(
                "SELECT m.name, p.name, p.type FROM sqlite_schema m JOIN pragma_table_info(m.name) p "
                "WHERE m.type IN ('table', 'view') AND p.name LIKE ? ESCAPE '\\' ORDER BY m.name, p.name",
                (like,), name)]
        return {"tables": tables[:100], "columns": columns[:100]}

    def overview(self, rows: Rows, db: str) -> dict[str, list[str]]:
        cols = rows("SELECT m.name, p.name, p.type, p.pk FROM sqlite_schema m JOIN pragma_table_info(m.name) p "
                    "WHERE m.type IN ('table', 'view') AND m.name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
                    "ORDER BY m.name, p.cid", None, db)
        fks = {(t, f): f"{rt}.{to}" for t, f, rt, to in rows(
            'SELECT m.name, f."from", f."table", f."to" FROM sqlite_schema m '
            "JOIN pragma_foreign_key_list(m.name) f WHERE m.type = 'table'", None, db)}
        tables: dict[str, list[str]] = {}
        for t, c, ty, pk in cols:
            tag = (" PK" if pk else "") + (f" FK->{fks[(t, c)]}" if (t, c) in fks else "")
            tables.setdefault(t, []).append(f"{c} {ty}{tag}")
        return tables

    def columns(self, rows: Rows, tables: set[tuple[str, str]]) -> dict:
        schema: dict = {}
        for db in sorted({d for d, _ in tables}):
            if db not in self.files:
                continue
            names = sorted(t for d, t in tables if d == db)
            marks = ", ".join("?" * len(names))
            for t, c, ty in rows(f"SELECT m.name, p.name, p.type FROM sqlite_schema m JOIN pragma_table_info(m.name) p "
                                 f"WHERE m.name IN ({marks}) ORDER BY m.name, p.cid", tuple(names), db):
                schema.setdefault(db, {}).setdefault(t, {})[c] = ty or "ANY"
        return schema
