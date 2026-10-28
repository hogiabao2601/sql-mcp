"""Engine adapters. Each engine is added only when its own conformance suite passes."""
from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError

from ..config import Profile
from .base import Dialect, ErrorKind
from .mariadb import MariaDB
from .mysql import MySQL
from .sqlite import SQLite
from .starrocks import StarRocks

DIALECTS: dict[str, type[Dialect]] = {"mysql": MySQL, "mariadb": MariaDB, "sqlite": SQLite,
                                       "starrocks": StarRocks}
try:                                    # psycopg is an optional dependency: pip install sql-mcp[postgres]
    from .postgres import Postgres
    DIALECTS["postgres"] = Postgres
except ImportError:
    pass


def dialect_for(profile: Profile) -> Dialect:
    if profile.engine == "postgres" and "postgres" not in DIALECTS:
        raise ToolError(f"Profile '{profile.name}': PostgreSQL needs psycopg. "
                        "Install with: pip install 'sql-mcp[postgres]'")
    cls = DIALECTS.get(profile.engine)
    if cls is None:
        raise ToolError(f"Profile '{profile.name}': engine '{profile.engine}' is not supported yet. "
                        f"Supported: {', '.join(DIALECTS)}.")
    return cls(profile)


__all__ = ["DIALECTS", "Dialect", "ErrorKind", "dialect_for"]
