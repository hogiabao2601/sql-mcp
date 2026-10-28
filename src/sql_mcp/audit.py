"""Optional audit log: one JSON line per tool call (REVIEW #9). Off unless SQL_MCP_AUDIT_LOG names a file.

Never logs literal values unless SQL_MCP_AUDIT_LITERALS=1: statements are recorded as a fingerprint (the parsed
statement with every literal replaced by ?), a hash and a length; engine error texts (which can echo values)
are not recorded at all. The file is created owner-only (0600).
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import threading
import time
from datetime import datetime, UTC

from sqlglot import exp

from .guard import Checked

_lock = threading.Lock()
current: contextvars.ContextVar[dict | None] = contextvars.ContextVar("sql_mcp_audit_event", default=None)


def enabled() -> bool:
    return bool(os.environ.get("SQL_MCP_AUDIT_LOG"))


def literals() -> bool:
    return os.environ.get("SQL_MCP_AUDIT_LITERALS") == "1"


def fingerprint(c: Checked) -> str:
    tree = c.tree.transform(lambda n: exp.Placeholder() if isinstance(n, exp.Literal) else n)
    return tree.sql(dialect=c.dialect)


def note_statement(text: str, checked: Checked | None) -> None:
    """Called by the SQL tools with the caller's statement (and its parse, when it parsed)."""
    event = current.get()
    if event is None:
        return
    event["sha256"] = hashlib.sha256(text.encode()).hexdigest()[:16]
    event["length"] = len(text)
    if checked is not None:
        event["statement"] = fingerprint(checked)
    if literals():
        event["sql"] = text


def record(event: dict) -> None:
    path = os.environ.get("SQL_MCP_AUDIT_LOG")
    if not path:
        return
    line = json.dumps(event, default=str, ensure_ascii=False) + "\n"
    with _lock:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)


def new_event(tool: str, request_id: str) -> dict:
    return {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "id": request_id, "tool": tool,
            "_t0": time.monotonic()}
