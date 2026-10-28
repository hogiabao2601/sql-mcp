"""mysql_server.py: compatibility entry point for the MySQL-only server (v2).

The code now lives in the `sql_mcp` package (src/sql_mcp). This file keeps two things working unchanged:
  * launching:   python mysql_server.py          (reports serverInfo mysql-server / 2.0.0)
  * importing:   the names tests and scripts use (run, get_profile, config, _pools, check_read_only, ...)

New installs should use the `sql-mcp` command instead (see README).
RULE #1 of stdio servers: never print() to stdout. Log to stderr only.
"""
import pathlib
import sys

try:
    import sql_mcp  # noqa: F401  (installed: pip install -e .)
except ImportError:  # running from a checkout without installing
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

from sql_mcp.server import *  # noqa: E402,F401,F403
from sql_mcp.server import _pools, build_server, log  # noqa: E402,F401
from sql_mcp.results import cell as _cell  # noqa: E402,F401

if __name__ == "__main__":
    log("starting on stdio (mysql_server.py compatibility entry point)")
    build_server("mysql-server", "2.0.0", label="mysql").run(transport="stdio")
