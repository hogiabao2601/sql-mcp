"""Option A spike: SQLAlchemy 2 Core (engine, pool, URL, inspect(), stream_results) + per-dialect session hooks."""
from __future__ import annotations

import sqlite3
import time

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import URL

from adapters_raw import sqlite_authorizer

SESSION_SQL = {
    "mysql": ["SET SESSION transaction_read_only=1", "SET SESSION max_execution_time={ms}"],
    "mariadb": ["SET SESSION transaction_read_only=1", "SET SESSION max_statement_time={s}"],
    "postgres": ["SET default_transaction_read_only = on", "SET statement_timeout = {ms}"],
}
DRIVER = {"mysql": "mysql+pymysql", "mariadb": "mysql+pymysql", "postgres": "postgresql+psycopg"}
SQLITE_DENY = {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE,
               sqlite3.SQLITE_DELETE, sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_PRAGMA}


class Adapter:
    def __init__(self, engine_name: str, cfg: dict):
        self.name = engine_name
        self.timeout_ms = cfg.get("timeout_ms", 5000)
        if engine_name == "sqlite":
            self.engine = create_engine(f"sqlite:///file:{cfg['path']}?mode=ro&uri=true", pool_size=cfg.get("pool_size", 3))
        else:
            url = URL.create(DRIVER[engine_name], username=cfg["user"], password=cfg["password"],
                             host=cfg["host"], port=cfg["port"], database=cfg.get("database"))
            self.engine = create_engine(url, pool_size=cfg.get("pool_size", 3), max_overflow=0,
                                        pool_pre_ping=True, pool_recycle=1800, pool_timeout=10)
        event.listen(self.engine, "connect", self._on_connect)

    def _on_connect(self, dbapi_conn, _record):
        if self.name == "sqlite":
            dbapi_conn.execute("PRAGMA query_only = ON")
            dbapi_conn.set_authorizer(sqlite_authorizer(SQLITE_DENY))
            return
        cur = dbapi_conn.cursor()
        for s in SESSION_SQL[self.name]:
            cur.execute(s.format(ms=self.timeout_ms, s=self.timeout_ms / 1000))
        cur.close()
        if self.name == "postgres":
            dbapi_conn.commit()          # psycopg is not in autocommit: keep the SETs

    def query(self, sql: str, max_rows: int = 100, invalidate_on_early_stop: bool = False) -> dict:
        with self.engine.connect() as conn:
            if self.name == "sqlite":
                deadline = time.monotonic() + self.timeout_ms / 1000
                conn.connection.dbapi_connection.set_progress_handler(
                    lambda: 1 if time.monotonic() > deadline else 0, 10_000)
            result = conn.execution_options(stream_results=True, max_row_buffer=200).execute(text(sql))
            cols = list(result.keys())
            rows, truncated = [], None
            for row in result:
                if len(rows) >= max_rows:
                    truncated = f"row limit {max_rows}"
                    break
                rows.append(list(row))
            if truncated and invalidate_on_early_stop:
                conn.invalidate()            # drop the connection instead of draining
            else:
                result.close()               # driver-dependent: drains on pymysql SSCursor
            conn.rollback()
            return {"columns": cols, "rows": rows, "truncated": truncated}

    def tables(self, schema):
        return sorted(inspect(self.engine).get_table_names(schema=None if self.name == "sqlite" else schema))

    def describe(self, schema, table):
        insp = inspect(self.engine)
        sch = None if self.name == "sqlite" else schema
        cols = [[c["name"], str(c["type"]), c["nullable"]] for c in insp.get_columns(table, schema=sch)]
        fk_out = [[",".join(f["constrained_columns"]), f["referred_table"], ",".join(f["referred_columns"])]
                  for f in insp.get_foreign_keys(table, schema=sch)]
        # SQLAlchemy has no "referenced by": scan every table's outgoing keys
        fk_in = [[t, ",".join(f["constrained_columns"])] for t in insp.get_table_names(schema=sch)
                 for f in insp.get_foreign_keys(t, schema=sch) if f["referred_table"] == table]
        return {"columns": cols, "fk_out": fk_out, "fk_in": fk_in}
