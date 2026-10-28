"""PostgreSQL through psycopg 3. Layer 2:

  * every statement runs in its own READ ONLY transaction that is always ROLLED BACK (psycopg
    `read_only=True`, not autocommit). A SELECT can call set_config() to switch read-only off for later
    transactions; the rollback undoes that (spike, ADR section 2.1), and the guard refuses set_config.
  * statement_timeout (ms) covers every statement. standard_conforming_strings=on pins how '...' literals
    are read, so the guard and the server agree. All three are startup options, set before any SQL runs.
"""
from __future__ import annotations

import itertools

import psycopg
from psycopg import errors as pgerr

from ..pool import Conn
from ..util import log
from .base import Dialect, ErrorKind, Rows

_RELKINDS = "('r', 'v', 'm', 'p', 'f')"          # tables, views, materialized views, partitioned, foreign
_SYSTEM = ("information_schema", "pg_catalog", "pg_toast")
_names = itertools.count()


class Postgres(Dialect):
    name, label, sqlglot = "postgres", "PostgreSQL", "postgres"
    system_schemas = _SYSTEM
    _warned: set[str] = set()

    def default_namespace(self) -> str | None:
        return self.p.schema or "public"

    def is_system(self, name: str) -> bool:
        n = name.lower()
        return n in _SYSTEM or n.startswith("pg_temp_") or n.startswith("pg_toast_temp_")

    # connections ------------------------------------------------------------------------------
    def _tls_args(self) -> dict:
        args = {"sslmode": self.p.tls}             # libpq has the same five modes, with the same meaning
        if self.p.tls_ca:
            args["sslrootcert"] = self.p.tls_ca
        return args

    def open(self) -> Conn:
        p = self.p
        options = (f"-c statement_timeout={p.timeout_ms} -c standard_conforming_strings=on "
                   f"-c default_transaction_read_only=on")
        raw = psycopg.connect(host=p.host, port=p.port, user=p.user, password=p.password, dbname=p.database,
                              connect_timeout=5, application_name="sql-mcp", options=options, **self._tls_args())
        raw.read_only = True             # every transaction starts as BEGIN READ ONLY
        raw.prepare_threshold = None     # no server-side prepared statements (works behind poolers too)
        if p.tls in ("prefer", "require") and p.name not in Postgres._warned:
            Postgres._warned.add(p.name)
            state = "encrypted but NOT verified" if raw.pgconn.ssl_in_use else "NOT encrypted"
            log(f"WARNING profile={p.name}: the connection is {state} (tls={p.tls}). "
                f"Set tls=verify-full and tls_ca to verify the server.")
        return Conn(raw, db=None)        # None = the server's default search_path

    def ping(self, raw) -> None:
        raw.execute("SELECT 1")
        raw.rollback()

    def use_database(self, conn: Conn, db: str | None) -> bool:
        """Here `db` is a SCHEMA: set search_path for the session, only when it changes. It is committed on
        its own, so the per-statement rollbacks keep it; a call with no schema gets the default back."""
        if conn.db == db:
            return True
        if db is None:
            conn.raw.execute("RESET search_path")
        else:
            conn.raw.execute("SELECT set_config('search_path', quote_ident(%s), false)", (db,))
        conn.raw.commit()
        conn.db = db
        return True

    # statements -------------------------------------------------------------------------------
    def execute(self, conn: Conn, sql: str, args, stream: bool):
        # A server-side cursor fetches in batches (only for SELECTs without our LIMIT); otherwise a plain one.
        cur = conn.raw.cursor(name=f"sqlmcp_{next(_names)}") if stream else conn.raw.cursor()
        cur.execute(sql, args)
        return cur

    def finish(self, conn: Conn, cursor, complete: bool) -> bool:
        cursor.close()          # a server-side cursor is CLOSEd: unread rows cost nothing
        conn.raw.rollback()     # end the read-only transaction (also undoes any set_config(..., false))
        return True

    def reset(self, conn: Conn) -> None:
        conn.raw.rollback()

    def error_kind(self, exc: BaseException) -> ErrorKind | None:
        if not isinstance(exc, psycopg.Error):
            return None
        if isinstance(exc, pgerr.QueryCanceled):
            return ErrorKind.TIMEOUT
        if isinstance(exc, pgerr.ReadOnlySqlTransaction):
            return ErrorKind.READ_ONLY
        if exc.sqlstate in ("57P01", "57P02", "57P03") or isinstance(exc, psycopg.InterfaceError):
            return ErrorKind.LOST          # terminated by an admin, crash, or shutdown
        if isinstance(exc, psycopg.OperationalError) and exc.sqlstate is None:
            # "connection failed: ..." happens while CONNECTING (refused, TLS, bad host): report it as is.
            return ErrorKind.OTHER if str(exc).startswith("connection failed") else ErrorKind.LOST
        return ErrorKind.OTHER

    def cancel(self, conn: Conn) -> None:
        conn.raw.cancel_safe()

    # names ------------------------------------------------------------------------------------
    def quote(self, name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def find_database(self, rows: Rows, name: str) -> str | None:
        r = rows("SELECT nspname FROM pg_namespace WHERE nspname = %s", (name,))
        return r[0][0] if r else None

    def find_table(self, rows: Rows, db: str, name: str) -> str | None:
        r = rows(f"SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                 f"WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN {_RELKINDS}", (db, name))
        return r[0][0] if r else None

    # introspection (pg_catalog; only what the account can read, like MySQL's information_schema) -------
    def databases(self, rows: Rows) -> list[str]:
        return [r[0] for r in rows("SELECT nspname FROM pg_namespace WHERE has_schema_privilege(oid, 'USAGE') "
                                   "ORDER BY nspname")]

    def tables_sql(self) -> str:
        return ("SELECT c.relname AS name, CASE c.relkind WHEN 'r' THEN 'BASE TABLE' WHEN 'v' THEN 'VIEW' "
                "WHEN 'm' THEN 'MATERIALIZED VIEW' WHEN 'p' THEN 'PARTITIONED TABLE' ELSE 'FOREIGN TABLE' END AS type, "
                "am.amname AS engine, GREATEST(c.reltuples, 0)::bigint AS est_rows, "
                "ROUND(pg_relation_size(c.oid) / 1048576.0, 2) AS data_mb, "
                "ROUND(pg_indexes_size(c.oid) / 1048576.0, 2) AS index_mb, "
                "obj_description(c.oid, 'pg_class') AS comment "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace LEFT JOIN pg_am am ON am.oid = c.relam "
                f"WHERE n.nspname = %s AND c.relkind IN {_RELKINDS} AND has_any_column_privilege(c.oid, 'SELECT') "
                "ORDER BY c.relname")

    _COLUMNS = ("SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
                "pg_get_expr(d.adbin, d.adrelid), a.attidentity <> '', col_description(c.oid, a.attnum), "
                "EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = c.oid AND i.indisprimary "
                "        AND a.attnum = ANY (i.indkey)), "
                "EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = c.oid AND i.indisunique AND NOT i.indisprimary "
                "        AND i.indnatts = 1 AND a.attnum = ANY (i.indkey)), c.relname "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
                "LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum "
                f"WHERE n.nspname = %s AND c.relkind IN {_RELKINDS} AND has_any_column_privilege(c.oid, 'SELECT') ")

    _FKS = ("SELECT a.attname, nf.nspname, cf.relname, af.attname, n.nspname, c.relname "
            "FROM pg_constraint k "
            "JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_class cf ON cf.oid = k.confrelid JOIN pg_namespace nf ON nf.oid = cf.relnamespace "
            "JOIN LATERAL unnest(k.conkey, k.confkey) AS u(a, b) ON true "
            "JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = u.a "
            "JOIN pg_attribute af ON af.attrelid = k.confrelid AND af.attnum = u.b "
            "WHERE k.contype = 'f' ")

    def describe(self, rows: Rows, db: str, table: str) -> dict:
        cols = rows(self._COLUMNS + "AND c.relname = %s ORDER BY a.attnum", (db, table))
        if not cols:
            return {}
        idx = rows("SELECT i.relname, NOT ix.indisunique, a.attname FROM pg_index ix "
                   "JOIN pg_class t ON t.oid = ix.indrelid JOIN pg_namespace n ON n.oid = t.relnamespace "
                   "JOIN pg_class i ON i.oid = ix.indexrelid "
                   "JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord) ON true "
                   "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
                   "WHERE n.nspname = %s AND t.relname = %s ORDER BY i.relname, k.ord", (db, table))
        fks = rows(self._FKS + "AND ((n.nspname = %s AND c.relname = %s) OR (nf.nspname = %s AND cf.relname = %s))",
                   (db, table, db, table))

        def col(c):  # same shape as MySQL: "id integer NOT NULL PRI identity"
            name, ctype, notnull, default, identity, comment, pk, uni, _ = c
            parts = [name, ctype]
            if notnull:
                parts.append("NOT NULL")
            if pk or uni:
                parts.append("PRI" if pk else "UNI")
            if default is not None:
                parts.append(f"DEFAULT {default}")
            if identity:
                parts.append("identity")
            if comment:
                parts.append(f"-- {comment}")
            return " ".join(parts)

        indexes: dict[str, list] = {}
        for name, non_unique, column in idx:
            indexes.setdefault(name, [non_unique, []])[1].append(column)
        return {
            "table": f"{db}.{table}",
            "columns": [col(c) for c in cols],
            "indexes": [f"{n}({','.join(cs)}){' UNIQUE' if not nu else ''}" for n, (nu, cs) in indexes.items()],
            "references": [f"{a} -> {fs}.{ft}.{fa}" for a, fs, ft, fa, s, t in fks if (s, t) == (db, table)],
            "referenced_by": [f"{s}.{t}.{a} -> {fa}" for a, fs, ft, fa, s, t in fks if (fs, ft) == (db, table)],
        }

    def search(self, rows: Rows, like: str, db: str | None) -> dict:
        scope, args = ("AND table_schema = %s", (like, db)) if db else (
            "AND table_schema NOT IN ('information_schema', 'pg_catalog') AND table_schema NOT LIKE 'pg\\_%%'", (like,))
        tables = rows(f"SELECT table_schema, table_name FROM information_schema.tables "
                      f"WHERE table_name ILIKE %s {scope} ORDER BY 1, 2 LIMIT 100", args)
        columns = rows(f"SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns "
                       f"WHERE column_name ILIKE %s {scope} ORDER BY 1, 2, 3 LIMIT 100", args)
        return {"tables": [f"{s}.{t}" for s, t in tables],
                "columns": [f"{s}.{t}.{c} {ty}" for s, t, c, ty in columns]}

    def overview(self, rows: Rows, db: str) -> dict[str, list[str]]:
        cols = rows(self._COLUMNS + "ORDER BY c.relname, a.attnum", (db,))
        fks = {(t, a): f"{ft}.{fa}" for a, fs, ft, fa, s, t in rows(self._FKS + "AND n.nspname = %s", (db,))}
        tables: dict[str, list[str]] = {}
        for name, ctype, _nn, _d, _i, _c, pk, _u, t in cols:
            tag = (" PK" if pk else "") + (f" FK->{fks[(t, name)]}" if (t, name) in fks else "")
            tables.setdefault(t, []).append(f"{name} {ctype}{tag}")
        return tables

    def columns(self, rows: Rows, tables: set[tuple[str, str]]) -> dict:
        schema: dict = {}
        if not tables:
            return schema
        pairs = sorted(tables)
        where = " OR ".join(["(table_schema = %s AND table_name = %s)"] * len(pairs))
        for db, t, c, ty in rows("SELECT table_schema, table_name, column_name, data_type "
                                 f"FROM information_schema.columns "
                                 f"WHERE {where} ORDER BY table_schema, table_name, ordinal_position",
                                 tuple(x for pair in pairs for x in pair)):
            schema.setdefault(db, {}).setdefault(t, {})[c] = ty
        return schema
