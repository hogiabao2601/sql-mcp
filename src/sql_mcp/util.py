"""Small shared helpers."""
from __future__ import annotations

import contextvars
import sys

from mcp.server.mcpserver.exceptions import ToolError

request_id: contextvars.ContextVar[str] = contextvars.ContextVar("sql_mcp_request_id", default="")


class Refused(ToolError):
    """The guard or the masking policy refused the statement (layer 1). Same message to the model."""


class QueryTimeout(ToolError):
    """The statement hit the time limit."""


def log(msg: str) -> None:
    rid = request_id.get()
    print(f"[sql-mcp]{f' [{rid}]' if rid else ''} {msg}", file=sys.stderr, flush=True)   # stdout = protocol channel
