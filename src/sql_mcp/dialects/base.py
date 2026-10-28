"""What every engine must provide. One instance per profile."""
from __future__ import annotations

from enum import Enum
from typing import Any
from collections.abc import Callable

from ..config import Profile
from ..pool import Conn

Rows = Callable[..., list[tuple]]     # rows_of(sql, args=None, database=None) -> rows (internal queries)


class ErrorKind(Enum):
    LOST = "lost"            # the connection died before or while the statement ran (KILL, restart, idle timeout)
    DEADLINE = "deadline"    # OUR socket read timeout fired: the statement may still be running on the server
    TIMEOUT = "timeout"      # the ENGINE stopped the statement at its time limit
    READ_ONLY = "read_only"  # the engine refused a write (layer 2)
    OTHER = "other"          # any other server error; the connection is still usable


class Dialect:
    name = "?"
    label = "?"                      # used in error messages, e.g. "MySQL error: ..."
    sqlglot = "?"
    system_schemas: tuple[str, ...] = ()
    engine_read_only = True          # False: the engine has NO read-only mode (StarRocks); see the README

    def __init__(self, profile: Profile):
        self.p = profile

    # connections ------------------------------------------------------------------------------
    def open(self) -> Conn: raise NotImplementedError
    def ping(self, raw: Any) -> None: raise NotImplementedError
    def close(self, raw: Any) -> None: raw.close()

    def default_namespace(self) -> str | None:
        """What a call means when it names no database: MySQL's default database, PostgreSQL's schema."""
        return self.p.database

    def is_system(self, name: str) -> bool:
        return name.lower() in self.system_schemas

    def use_database(self, conn: Conn, db: str | None) -> bool:
        """Make `db` the session's current database. False = this connection cannot serve that
        (e.g. it has a database selected and the caller wants none): the caller gets a fresh one."""
        raise NotImplementedError

    # statements -------------------------------------------------------------------------------
    def execute(self, conn: Conn, sql: str, args, stream: bool) -> Any:
        """Start the statement and return a cursor with .description and .fetchmany(n). stream=True: the
        result may be large and has no LIMIT of ours, so fetch it incrementally if the engine can."""
        raise NotImplementedError

    def explain_prefix(self) -> str:
        return "EXPLAIN "

    def reset(self, conn: Conn) -> None:
        """Make the connection usable again after a statement failed (e.g. end an aborted transaction)."""

    def column_origins(self, cursor: Any, n: int) -> list[str]:
        """Original (pre-alias) column names, when the driver reports them; '' otherwise."""
        return [""] * n

    def finish(self, conn: Conn, cursor: Any, complete: bool) -> bool:
        """End the statement. complete=False means rows were left unread. True = connection reusable."""
        raise NotImplementedError

    def error_kind(self, exc: BaseException) -> ErrorKind | None:
        """Classify a driver exception; None = not a database error at all."""
        raise NotImplementedError

    def cancel(self, conn: Conn) -> None:
        """Stop the statement still running for this connection (from a side connection)."""

    # names --------------------------------------------------------------------------------------
    def quote(self, name: str) -> str: raise NotImplementedError
    def find_database(self, rows: Rows, name: str) -> str | None:
        """The catalog's exact spelling of database `name`, or None. Parameterized: any text is safe."""
        raise NotImplementedError
    def find_table(self, rows: Rows, db: str, name: str) -> str | None: raise NotImplementedError

    # introspection ----------------------------------------------------------------------------
    def databases(self, rows: Rows) -> list[str]: raise NotImplementedError
    TABLE_COLUMNS = ("name", "type", "engine", "est_rows", "data_mb", "index_mb", "comment")

    def tables_sql(self) -> str: raise NotImplementedError

    def fetch_tables(self, rows: Rows, db: str) -> dict:
        """{columns, rows} for list_tables. Default: run tables_sql against information_schema."""
        return {"columns": list(self.TABLE_COLUMNS), "rows": [list(r) for r in rows(self.tables_sql(), (db,), db)]}
    def describe(self, rows: Rows, db: str, table: str) -> dict: raise NotImplementedError
    def search(self, rows: Rows, like: str, db: str | None) -> dict: raise NotImplementedError
    def overview(self, rows: Rows, db: str) -> dict[str, list[str]]:
        """{table: ['col type PK FK->t.c', ...]} in a stable order; the server formats and caps it."""
        raise NotImplementedError

    def columns(self, rows: Rows, tables: set[tuple[str, str]]) -> dict:
        """{db: {table: {column: type}}} for the given (db, table) pairs: the schema masking qualifies against."""
        raise NotImplementedError
