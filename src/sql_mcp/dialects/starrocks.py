"""StarRocks through PyMySQL (MySQL protocol). READ THIS before using it:

  * StarRocks has NO read-only mode. `transaction_read_only` is accepted but not enforced, and there is no
    START TRANSACTION READ ONLY. Layer 2 for writes does not exist: only the account (layer 3) and the
    guard (layer 1) stop a write.
  * A SELECT-only account can still WRITE FILES: INSERT INTO FILES(...), SELECT ... INTO OUTFILE, and the
    FILES() table function reach any file / S3 / HDFS location, with credentials given INSIDE the statement
    (so removing credentials from the backends is not enough: block egress by network policy). The guard
    refuses all of these, including FILES() inside a SELECT. Verified on 4.1.1 / 3.5.12.
  * The time limit is `query_timeout`, in whole SECONDS (rounded up, at least 1). Any user may raise it with
    SET or a /*+ SET_VAR(...) */ hint: the guard refuses both, and the client deadline still applies.
  * No foreign keys and no index catalog: describe_table shows the primary key only.

Catalogs: a profile may set `catalog` (default `default_catalog`). `default_catalog` is introspected through
information_schema. An EXTERNAL catalog (Iceberg, Hive, ...) is not in information_schema for a least-privilege
user, so it is introspected with SHOW / DESC, qualified by the catalog. Masking resolves external columns the
same way (DESC), so an aliased secret column is still masked.
"""
from __future__ import annotations

import math

from ..pool import Conn
from ..util import log
from .base import Rows
from .mysql import MySQL


class StarRocks(MySQL):
    name, label, sqlglot = "starrocks", "StarRocks", "starrocks"
    system_schemas = ("information_schema", "_statistics_", "sys")
    timeout_codes = {5024}               # "Query reached its timeout of N seconds"
    engine_read_only = False
    _warned_ro: set[str] = set()

    def __init__(self, profile):
        super().__init__(profile)
        self.catalog = profile.catalog or "default_catalog"
        self.external = self.catalog != "default_catalog"

    def session_sql(self) -> str:
        # Not transaction_read_only: StarRocks accepts it and ignores it, which would only look like protection.
        return f"SET query_timeout = {max(1, math.ceil(self.p.timeout_ms / 1000))}"

    def open(self) -> Conn:
        # For an external catalog the profile's database lives in that catalog, not in default_catalog, so do
        # not select it at connect time: SET CATALOG first, then USE.
        raw = self._connect(init_command=self.session_sql(), **({"database": None} if self.external else {}))
        if self.external:
            with raw.cursor() as c:
                c.execute(f"SET CATALOG {self.quote(self.catalog)}")
                if self.p.database:
                    c.execute(f"USE {self.quote(self.p.database)}")
        if self.p.name not in StarRocks._warned_ro:
            StarRocks._warned_ro.add(self.p.name)
            log(f"WARNING profile={self.p.name}: StarRocks has NO read-only mode. Only the database account and the "
                "guard stop writes, and a SELECT-only account can still export files (INSERT INTO FILES / FILES()). "
                "See README, 'Supported engines'.")
        return Conn(raw, db=self.p.database)

    def use_database(self, conn: Conn, db: str | None) -> bool:
        if not self.external:
            return super().use_database(conn, db)
        target = db or self.p.database
        if conn.db != target:
            with conn.raw.cursor() as c:
                c.execute(f"SET CATALOG {self.quote(self.catalog)}")   # cheap; keeps the session on this catalog
                if target:
                    c.execute(f"USE {self.quote(target)}")
            conn.db = target
        return True

    # introspection: default_catalog via information_schema (inherited), external via SHOW / DESC ----------
    def _q(self, db: str | None = None, table: str | None = None) -> str:
        parts = [self.catalog] + ([db] if db else []) + ([table] if table else [])
        return ".".join(self.quote(p) for p in parts)

    def _desc(self, rows: Rows, db: str, table: str) -> list[tuple]:
        # DESC <catalog>.<db>.<table> -> (name, type, nullable 'Yes'/'No', key 'true'/'false', default, extra, ...)
        return rows(f"DESC {self._q(db, table)}")

    def databases(self, rows: Rows) -> list[str]:
        if not self.external:
            return super().databases(rows)
        return [r[0] for r in rows(f"SHOW DATABASES FROM {self.quote(self.catalog)}")]

    def is_system(self, name: str) -> bool:
        return not self.external and super().is_system(name)

    def fetch_tables(self, rows: Rows, db: str) -> dict:
        if not self.external:
            return super().fetch_tables(rows, db)
        names = [r[0] for r in rows(f"SHOW TABLES FROM {self._q(db)}")]
        # No sizes or row estimates for external tables (like SQLite): those columns are null.
        return {"columns": list(self.TABLE_COLUMNS),
                "rows": [[n, "EXTERNAL TABLE", self.catalog, None, None, None, None] for n in names]}

    def find_database(self, rows: Rows, name: str) -> str | None:
        if not self.external:
            return super().find_database(rows, name)
        return next((r[0] for r in rows(f"SHOW DATABASES FROM {self.quote(self.catalog)}") if r[0] == name), None)

    def find_table(self, rows: Rows, db: str, name: str) -> str | None:
        if not self.external:
            return super().find_table(rows, db, name)
        return next((r[0] for r in rows(f"SHOW TABLES FROM {self._q(db)}") if r[0] == name), None)

    def describe(self, rows: Rows, db: str, table: str) -> dict:
        if not self.external:
            out = super().describe(rows, db, table)
            if out and not out["indexes"]:       # StarRocks leaves information_schema.statistics empty
                pk = [r[0] for r in rows("SELECT column_name FROM information_schema.columns WHERE table_schema=%s "
                                         "AND table_name=%s AND column_key='PRI' ORDER BY ordinal_position",
                                         (db, table))]
                if pk:
                    out["indexes"] = [f"PRIMARY({','.join(pk)}) UNIQUE"]
            return out
        cols = self._desc(rows, db, table)
        if not cols:
            return {}
        pk = [c[0] for c in cols if str(c[3]).lower() == "true"]

        def col(c):
            name, ctype, nullable = c[0], c[1], c[2]
            parts = [name, ctype]
            if str(nullable).lower() in ("no", "false"):
                parts.append("NOT NULL")
            if str(c[3]).lower() == "true":
                parts.append("PRI")
            return " ".join(parts)
        return {"table": f"{self.catalog}.{db}.{table}", "columns": [col(c) for c in cols],
                "indexes": [f"PRIMARY({','.join(pk)}) UNIQUE"] if pk else [],
                "references": [], "referenced_by": []}       # external catalogs have no foreign keys

    def search(self, rows: Rows, like: str, db: str | None) -> dict:
        if not self.external:
            return super().search(rows, like, db)
        # The server passed a LIKE pattern with wildcards escaped; search_schema is a plain "name contains".
        needle = like.strip("%").replace("\\%", "%").replace("\\_", "_").lower()
        dbs = [db] if db else self.databases(rows)
        tables: list[str] = []
        columns: list[str] = []
        for d in dbs:
            names = [t for (t,) in rows(f"SHOW TABLES FROM {self._q(d)}")]
            tables += [f"{d}.{t}" for t in names if needle in t.lower()]
            for t in names:
                if len(columns) >= 100:
                    break
                columns += [f"{d}.{t}.{c[0]} {c[1]}" for c in self._desc(rows, d, t) if needle in c[0].lower()]
        return {"tables": tables[:100], "columns": columns[:100]}

    def overview(self, rows: Rows, db: str) -> dict[str, list[str]]:
        if not self.external:
            return super().overview(rows, db)
        tables: dict[str, list[str]] = {}
        for (t,) in rows(f"SHOW TABLES FROM {self._q(db)}"):
            for c in self._desc(rows, db, t):
                tag = " PK" if str(c[3]).lower() == "true" else ""
                tables.setdefault(t, []).append(f"{c[0]} {c[1]}{tag}")
        return tables

    def columns(self, rows: Rows, tables: set[tuple[str, str]]) -> dict:
        if not self.external:
            return super().columns(rows, tables)
        schema: dict = {}
        for db, t in sorted(tables):
            for c in self._desc(rows, db, t):
                schema.setdefault(db, {}).setdefault(t, {})[c[0]] = c[1]
        return schema
