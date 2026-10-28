"""A small thread-safe connection pool. Tools run in worker threads (the SDK calls sync tools via
anyio.to_thread), hence the locking.

Capacity is guarded by a Condition, so a waiter wakes up both when a connection is RELEASED and
when one is DISCARDED (REVIEW #1: waiting only on the idle queue starved waiters for 10 s).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

IDLE_PING_S = 30          # servers close idle connections (wait_timeout); only ping one that sat idle this long
MAX_LIFETIME_S = 1800     # recycle connections older than this, at checkout
WAIT_S = 10               # how long a call waits for a busy pool


@dataclass
class Conn:
    """A pooled connection plus the state we track ourselves (never attributes on the driver object)."""
    raw: Any
    born: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)
    db: str | None = None         # the session's current database, as WE last set it


class Pool:
    def __init__(self, name: str, size: int, open_fn: Callable[[], Conn], ping_fn: Callable[[Any], None],
                 close_fn: Callable[[Any], None]):
        self.name, self.size = name, size
        self._open, self._ping, self._close = open_fn, ping_fn, close_fn
        self._idle: deque[Conn] = deque()
        self._created = 0
        self._cv = threading.Condition()

    def acquire(self) -> Conn:
        deadline = time.monotonic() + WAIT_S
        while True:
            conn = self._checkout(deadline)
            if conn is not None:
                return conn
            try:
                return self._open()
            except Exception:
                self._forget()
                raise

    def _checkout(self, deadline: float) -> Conn | None:
        """An idle connection that is still good, or None when the caller may open a new one."""
        while True:
            with self._cv:
                while not self._idle and self._created >= self.size:
                    if not self._cv.wait(deadline - time.monotonic()):
                        raise ToolError(f"All {self.size} connections of profile '{self.name}' are busy.")
                if not self._idle:
                    self._created += 1
                    return None
                conn = self._idle.pop()                 # LIFO: the most recently used is the most likely alive
            now = time.monotonic()
            if now - conn.born > MAX_LIFETIME_S:
                self.discard(conn)
                continue
            if now - conn.last_used > IDLE_PING_S:
                try:
                    self._ping(conn.raw)
                except Exception:                       # dead: drop it and try the next one
                    self.discard(conn)
                    continue
            return conn

    def release(self, conn: Conn) -> None:
        conn.last_used = time.monotonic()
        with self._cv:
            self._idle.append(conn)
            self._cv.notify()

    def discard(self, conn: Conn) -> None:
        try:
            self._close(conn.raw)
        except Exception:     # a connection that is already broken may fail to close; that is fine
            pass
        self._forget()

    def _forget(self) -> None:
        with self._cv:
            self._created -= 1
            self._cv.notify()

    def purge_idle(self) -> None:
        """Drop every idle connection. When one connection is dead because the server restarted
        or killed sessions, the others sitting in the pool are almost certainly dead too."""
        while True:
            with self._cv:
                if not self._idle:
                    return
                conn = self._idle.pop()
            self.discard(conn)
