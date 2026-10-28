"""MariaDB through PyMySQL. Same protocol, catalog and SQL as MySQL, except layer 2:

  * read-only: `transaction_read_only` from 11.1 (`tx_read_only` before; it is deprecated from 11.1),
  * time limit: `max_statement_time`, in SECONDS (fractions allowed), and it covers EVERY statement
    (MySQL's max_execution_time covers SELECT only). MariaDB has no max_execution_time at all.
The version decides the variable name, so the session is set right after connecting instead of
through init_command.
"""
from __future__ import annotations

import re

import pymysql

from .mysql import MySQL


def server_version(raw) -> tuple[int, int]:
    """(major, minor) from e.g. '11.4.13-MariaDB-ubu2404' or the old-client form '5.5.5-10.11.19-MariaDB'."""
    v = re.sub(r"^5\.5\.5-", "", raw.server_version or "")
    m = re.match(r"(\d+)\.(\d+)", v)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


class MariaDB(MySQL):
    name, label, sqlglot = "mariadb", "MariaDB", "mysql"
    timeout_codes = {1969}               # "Query execution was interrupted (max_statement_time exceeded)"

    def session_sql(self, version: tuple[int, int] = (11, 4)) -> str:
        read_only = "transaction_read_only" if version >= (11, 1) else "tx_read_only"
        return (f"SET SESSION {read_only}=1, SESSION max_statement_time={self.p.timeout_ms / 1000:g}, "
                f"{self.PIN_SQL_MODE}")

    def _connect_session(self) -> pymysql.connections.Connection:
        raw = self._connect()
        try:
            with raw.cursor() as cur:
                cur.execute(self.session_sql(server_version(raw)))
        except Exception:
            raw.close()
            raise
        return raw
