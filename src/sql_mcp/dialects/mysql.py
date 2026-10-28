"""MySQL through PyMySQL. Layer 2: transaction_read_only=1 and max_execution_time, set on every
(re)connect by init_command, so a reconnect can never forget them."""
from __future__ import annotations

import pymysql
from pymysql.cursors import SSCursor

from ..pool import Conn
from ..util import log
from .base import Dialect, ErrorKind, Rows

_LOST_CODES = {2006, 2013, 2055}   # server has gone away / lost connection during query / lost during read


class MySQL(Dialect):
    name, label, sqlglot = "mysql", "MySQL", "mysql"
    system_schemas: tuple[str, ...] = ("information_schema", "mysql", "performance_schema", "sys")
    timeout_codes = {3024}

    # The guard tokenizes with backslash escapes on and "..." as a string; make the session agree (REVIEW #8).
    PIN_SQL_MODE = ("SESSION sql_mode = TRIM(BOTH ',' FROM REPLACE(REPLACE(CONCAT(',', @@SESSION.sql_mode, ','), "
                    "',NO_BACKSLASH_ESCAPES,', ','), ',ANSI_QUOTES,', ','))")

    def session_sql(self) -> str:
        return (f"SET SESSION transaction_read_only=1, SESSION max_execution_time={self.p.timeout_ms}, "
                f"{self.PIN_SQL_MODE}")

    def _tls_args(self) -> dict:
        """PyMySQL 1.2: no ssl args = 'preferred' (TLS if offered, CERT_NONE, silent plaintext fallback);
        a non-empty ssl dict = TLS required, raising if the server does not offer it."""
        mode, ca = self.p.tls, self.p.tls_ca
        if mode == "disable":
            return {"ssl_disabled": True}
        if mode == "prefer":
            return {}
        if mode == "require":
            return {"ssl": {"verify_mode": False, "check_hostname": False}}
        return {"ssl": {"ca": ca, "check_hostname": mode == "verify-full"}}

    def _connect(self, **extra) -> pymysql.connections.Connection:
        p = self.p
        extra.setdefault("database", p.database)      # StarRocks passes database=None for an external catalog
        return pymysql.connect(
            host=p.host, port=p.port or 3306, user=p.user, password=p.password,
            charset="utf8mb4", autocommit=True, connect_timeout=5,
            read_timeout=max(15, p.timeout_ms // 1000 + 5), **self._tls_args(), **extra)

    _warned: set[str] = set()

    def _connect_session(self) -> pymysql.connections.Connection:
        """A connection with layer 2 applied. init_command runs on every (re)connect, so it cannot be forgotten."""
        return self._connect(init_command=self.session_sql())

    def open(self) -> Conn:
        raw = self._connect_session()
        if self.p.tls in ("prefer", "require") and self.p.name not in MySQL._warned:
            MySQL._warned.add(self.p.name)
            state = "encrypted but NOT verified" if getattr(raw, "_secure", False) else "NOT encrypted"
            log(f"WARNING profile={self.p.name}: the connection is {state} (tls={self.p.tls}). "
                f"Set tls=verify-full and tls_ca to verify the server.")
        return Conn(raw, db=self.p.database)

    def ping(self, raw) -> None:
        raw.ping(reconnect=False)

    def use_database(self, conn: Conn, db: str | None) -> bool:
        if db is None:
            return conn.db is None          # MySQL cannot unselect a database: needs a fresh connection
        if conn.db != db:                   # tracked as str; PyMySQL's own conn.db is bytes (REVIEW #18)
            conn.raw.select_db(db)          # a protocol command, not SQL, so a bad name cannot inject
            conn.db = db
        return True

    def execute(self, conn: Conn, sql: str, args, stream: bool):
        cur = conn.raw.cursor(SSCursor)     # always unbuffered: rows stream, we stop reading at the cap
        cur.execute(sql, args)
        return cur

    def column_origins(self, cursor, n: int) -> list[str]:
        # MySQL reports a column's ORIGINAL name even when aliased. PyMySQL keeps it on the result's
        # field descriptors; fall back to labels only if that internal shape changes.
        fields = getattr(getattr(cursor, "_result", None), "fields", None) or []
        return [(getattr(fields[i], "org_name", "") or "") if i < len(fields) else "" for i in range(n)]

    def finish(self, conn: Conn, cursor, complete: bool) -> bool:
        if not complete:
            # Unread rows are still queued on the socket and could be millions: drop the connection.
            # (execute_query pushes a LIMIT into the statement, so this happens only on other paths.)
            # Tell PyMySQL the result is finished first, or its finalizers later try to drain the closed
            # socket and print an ignored AttributeError on stderr (REVIEW #16).
            result = getattr(cursor, "_result", None)
            if result is not None and hasattr(result, "unbuffered_active"):
                result.unbuffered_active = False
            cursor._result = None
            return False
        cursor.close()          # reads whatever is left: at most the one row past a pushed-down LIMIT
        return True

    def error_kind(self, exc: BaseException) -> ErrorKind | None:
        if not isinstance(exc, pymysql.MySQLError):
            return None
        code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
        if isinstance(exc, pymysql.err.InterfaceError):
            return ErrorKind.LOST
        if code in _LOST_CODES:
            return ErrorKind.DEADLINE if "timed out" in str(exc) else ErrorKind.LOST
        if code in self.timeout_codes:
            return ErrorKind.TIMEOUT
        if code == 1792:
            return ErrorKind.READ_ONLY
        return ErrorKind.OTHER

    def cancel(self, conn: Conn) -> None:
        side = self._connect()
        try:
            with side.cursor() as cur:
                cur.execute(f"KILL QUERY {int(conn.raw.thread_id())}")
        finally:
            side.close()

    # names (REVIEW #12) ------------------------------------------------------------------------
    def quote(self, name: str) -> str:
        return "`" + name.replace("`", "``") + "`"

    def find_database(self, rows: Rows, name: str) -> str | None:
        r = rows("SELECT schema_name FROM information_schema.schemata WHERE schema_name = %s", (name,))
        return next((x[0] for x in r if x[0] == name), None)            # exact, case-sensitive match only

    def find_table(self, rows: Rows, db: str, name: str) -> str | None:
        r = rows("SELECT table_name FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
                 (db, name))
        return next((x[0] for x in r if x[0] == name), None)

    # introspection (information_schema) -------------------------------------------------------
    def databases(self, rows: Rows) -> list[str]:
        return [r[0] for r in rows("SELECT schema_name FROM information_schema.schemata ORDER BY schema_name")]

    def tables_sql(self) -> str:
        return ("SELECT table_name AS name, table_type AS type, engine, table_rows AS est_rows, "
                "ROUND(data_length/1048576,2) AS data_mb, ROUND(index_length/1048576,2) AS index_mb, "
                "table_comment AS comment FROM information_schema.tables "
                "WHERE table_schema=%s ORDER BY table_name")

    def describe(self, rows: Rows, db: str, table: str) -> dict:
        cols = rows("SELECT column_name, column_type, is_nullable, column_key, column_default, extra, column_comment "
                    "FROM information_schema.columns WHERE table_schema=%s AND table_name=%s "
                    "ORDER BY ordinal_position", (db, table))
        if not cols:
            return {}
        idx = rows("SELECT index_name, non_unique, column_name FROM information_schema.statistics "
                   "WHERE table_schema=%s AND table_name=%s ORDER BY index_name, seq_in_index", (db, table))
        out_fk = rows("SELECT column_name, referenced_table_schema, referenced_table_name, referenced_column_name "
                      "FROM information_schema.key_column_usage WHERE table_schema=%s AND table_name=%s "
                      "AND referenced_table_name IS NOT NULL", (db, table))
        in_fk = rows("SELECT table_schema, table_name, column_name, referenced_column_name "
                     "FROM information_schema.key_column_usage WHERE referenced_table_schema=%s "
                     "AND referenced_table_name=%s", (db, table))

        def col(c):  # one compact line per column, e.g. "id int NOT NULL PRI auto_increment"
            name, ctype, nullable, key, default, extra, comment = c
            parts = [name, ctype]
            if nullable == "NO":
                parts.append("NOT NULL")
            if key:
                parts.append(key)
            if default is not None:
                parts.append(f"DEFAULT {default}")
            if extra:
                parts.append(extra)
            if comment:
                parts.append(f"-- {comment}")
            return " ".join(parts)

        indexes: dict[str, list] = {}
        for name, non_unique, column in idx:
            indexes.setdefault(name, [int(non_unique), []])[1].append(column)
        return {
            "table": f"{db}.{table}",
            "columns": [col(c) for c in cols],
            "indexes": [f"{n}({','.join(cs)}){' UNIQUE' if not nu else ''}" for n, (nu, cs) in indexes.items()],
            "references": [f"{c} -> {s}.{t}.{rc}" for c, s, t, rc in out_fk],      # this table points at ...
            "referenced_by": [f"{s}.{t}.{c} -> {rc}" for s, t, c, rc in in_fk],    # ... and is pointed at by
        }

    def search(self, rows: Rows, like: str, db: str | None) -> dict:
        scope, args = ("AND table_schema=%s", (like, db)) if db else (
            "AND table_schema NOT IN (%s,%s,%s,%s)", (like, *self.system_schemas))
        tables = rows(f"SELECT table_schema, table_name FROM information_schema.tables "
                      f"WHERE table_name LIKE %s {scope} ORDER BY 1,2 LIMIT 100", args)
        columns = rows(f"SELECT table_schema, table_name, column_name, column_type FROM information_schema.columns "
                       f"WHERE column_name LIKE %s {scope} ORDER BY 1,2,3 LIMIT 100", args)
        return {"tables": [f"{s}.{t}" for s, t in tables],
                "columns": [f"{s}.{t}.{c} {ty}" for s, t, c, ty in columns]}

    def columns(self, rows: Rows, tables: set[tuple[str, str]]) -> dict:
        schema: dict = {}
        if not tables:
            return schema
        pairs = sorted(tables)
        where = " OR ".join(["(table_schema=%s AND table_name=%s)"] * len(pairs))
        for db, t, c, ty in rows("SELECT table_schema, table_name, column_name, data_type "
                                 f"FROM information_schema.columns "
                                 f"WHERE {where} ORDER BY table_schema, table_name, ordinal_position",
                                 tuple(x for pair in pairs for x in pair)):
            schema.setdefault(db, {}).setdefault(t, {})[c] = ty
        return schema

    def overview(self, rows: Rows, db: str) -> dict[str, list[str]]:
        cols = rows("SELECT table_name, column_name, column_type, column_key FROM information_schema.columns "
                    "WHERE table_schema=%s ORDER BY table_name, ordinal_position", (db,))
        fks = {(t, c): f"{rt}.{rc}" for t, c, rt, rc in rows(
            "SELECT table_name, column_name, referenced_table_name, referenced_column_name "
            "FROM information_schema.key_column_usage WHERE table_schema=%s "
            "AND referenced_table_name IS NOT NULL", (db,))}
        tables: dict[str, list[str]] = {}
        for t, c, ty, key in cols:
            tag = (" PK" if key == "PRI" else "") + (f" FK->{fks[(t, c)]}" if (t, c) in fks else "")
            tables.setdefault(t, []).append(f"{c} {ty}{tag}")
        return tables
