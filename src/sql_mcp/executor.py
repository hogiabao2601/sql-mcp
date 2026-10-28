"""Running statements: pooling per profile, streaming with real caps, masking, readable errors, and
one retry ONLY when the connection died (never when our own deadline fired: REVIEW #3)."""
from __future__ import annotations

import os
import threading
import time

from mcp.server.mcpserver.exceptions import ToolError

from .config import Profile, on_reload
from .dialects import Dialect, ErrorKind, dialect_for
from .pool import Conn, Pool
from .results import DEFAULT_ROWS, INTERNAL_MAX_CHARS, INTERNAL_MAX_ROWS, MAX_CHARS, collect
from .util import QueryTimeout, log


_pools: dict[str, tuple[Dialect, Pool]] = {}
_pools_lock = threading.Lock()


def engine_for(p: Profile) -> tuple[Dialect, Pool]:
    with _pools_lock:
        if p.name not in _pools:
            d = dialect_for(p)
            _pools[p.name] = (d, Pool(p.name, p.pool_size, d.open, d.ping, d.close))
        return _pools[p.name]


def close_pools() -> None:
    """Close every idle pooled connection and forget the pools (config reloads, tests). Connections busy in a
    call are closed when that call releases them into the forgotten pool and the pool is garbage-collected."""
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for _d, pool in pools:
        pool.purge_idle()


on_reload(close_pools)            # a changed profile file must not keep connections made with old settings


def _friendly(d: Dialect, kind: ErrorKind, exc: BaseException) -> ToolError:
    if kind in (ErrorKind.TIMEOUT, ErrorKind.DEADLINE):
        return QueryTimeout(f"Query exceeded the {d.p.timeout_ms} ms limit. Add filters, a LIMIT, or an index.")
    if kind == ErrorKind.READ_ONLY:
        return ToolError("Rejected: this connection is read-only.")
    return ToolError(f"{d.label} error: {exc}")


class _ConnectionLost(Exception):
    """The pooled connection was dead (server restart, KILL, wait_timeout). Safe to retry once."""


def run(p: Profile, sql: str, args=None, database: str | None = None,
        max_rows: int = DEFAULT_ROWS, mask: bool = True, capped: bool = False,
        mask_columns: frozenset[int] = frozenset(), hide_errors: bool = False, max_chars: int = MAX_CHARS,
        stream: bool = False) -> dict:
    """Run a read statement; if the pooled connection turns out to be dead, retry ONCE on a fresh one.
    Only safe because every statement here is read-only (re-running it changes nothing).
    capped=True: the statement carries LIMIT max_rows+1, so unread rows after a row-limit stop are cheap to drain.
    mask_columns: output positions to mask (masking.plan); hide_errors: replace the engine's error text."""
    try:
        return _run_once(p, sql, args, database, max_rows, mask, capped, mask_columns, hide_errors, max_chars,
                         stream)
    except _ConnectionLost:
        log(f"profile={p.name} connection was lost, purging idle connections and retrying once")
        engine_for(p)[1].purge_idle()
        try:
            return _run_once(p, sql, args, database, max_rows, mask, capped, mask_columns, hide_errors,
                             max_chars, stream)
        except _ConnectionLost as exc:
            raise ToolError(f"{engine_for(p)[0].label} connection was lost twice in a row. Is the server up?") from exc


def _checkout(d: Dialect, pool: Pool, target: str | None) -> Conn:
    """A connection whose current database is `target` (REVIEW #2: never reuse another call's database)."""
    for _ in range(pool.size + 1):
        conn = pool.acquire()
        try:
            if d.use_database(conn, target):
                return conn
        except Exception:
            pool.discard(conn)
            raise
        pool.discard(conn)          # it has a database selected and the caller wants none: open a fresh one
    raise ToolError(f"Could not get a connection without a default database for profile '{d.p.name}'.")


def _run_once(p: Profile, sql: str, args, database: str | None, max_rows: int, mask: bool, capped: bool,
              mask_columns: frozenset[int], hide_errors: bool, max_chars: int, stream: bool) -> dict:
    d, pool = engine_for(p)
    started = time.monotonic()
    try:
        conn = _checkout(d, pool, database or d.default_namespace())
    except ToolError:
        raise
    except Exception as exc:
        kind = d.error_kind(exc)
        if kind is None:
            raise
        if kind == ErrorKind.LOST:
            raise _ConnectionLost() from exc
        raise _friendly(d, kind, exc) from exc

    reusable = False
    try:
        cur = d.execute(conn, sql, args, stream)
        columns = [c[0] for c in cur.description] if cur.description else []
        origins = d.column_origins(cur, len(columns))
        masked = {i for i, c in enumerate(columns)
                  if mask and (i in mask_columns or any(m in f"{c} {origins[i]}".lower() for m in p.mask))}
        rows, truncated = collect(cur.fetchmany, masked, max_rows, max_chars)
        drainable = truncated is None or (capped and truncated.startswith("row limit"))
        reusable = d.finish(conn, cur, complete=drainable)

        result = {"columns": columns, "rows": rows, "row_count": len(rows),
                  "ms": round((time.monotonic() - started) * 1000)}
        if truncated:
            result["truncated"] = truncated
        if masked:
            result["masked"] = [columns[i] for i in sorted(masked)]
        if os.environ.get("SQL_MCP_LOG_QUERIES", os.environ.get("MYSQL_LOG_QUERIES")) == "1":
            log(f"profile={p.name} ms={result['ms']} rows={len(rows)} sql={sql[:200]!r}")
        else:
            log(f"profile={p.name} ms={result['ms']} rows={len(rows)}")
        return result
    except Exception as exc:
        kind = d.error_kind(exc)
        if kind is None:
            raise
        if kind == ErrorKind.LOST:
            raise _ConnectionLost() from exc
        if kind == ErrorKind.DEADLINE:
            # Our socket timeout fired but the statement may still be running: stop it, never re-run it.
            try:
                d.cancel(conn)
            except Exception as cancel_exc:     # best effort: report the timeout either way
                log(f"profile={p.name} could not cancel the timed-out statement: {cancel_exc}")
        else:
            try:
                d.reset(conn)   # e.g. PostgreSQL: end the aborted transaction
                reusable = True # the server answered with an error: the connection is still usable (REVIEW #6)
            except Exception:
                reusable = False
        if hide_errors and kind == ErrorKind.OTHER:
            raise ToolError("The query failed. The database's error text is hidden because the query reads a "
                            "masked column (error messages can contain its values).") from exc
        raise _friendly(d, kind, exc) from exc
    finally:
        if reusable:
            pool.release(conn)
        else:
            pool.discard(conn)


def rows_of(p: Profile, sql: str, args=None, database: str | None = None) -> list[tuple]:
    """Internal helper for our own catalog queries: no masking, a far bigger cap, and never a silent cut
    (REVIEW #5: a truncated catalog read used to drop tables from describe/search/schema:// unnoticed)."""
    r = run(p, sql, args, database, max_rows=INTERNAL_MAX_ROWS, mask=False, max_chars=INTERNAL_MAX_CHARS)
    if r.get("truncated"):
        raise ToolError(f"Catalog result too large ({r['truncated']}). Narrow it down: pass a database, "
                        "or use search_schema / describe_table.")
    return [tuple(x) for x in r["rows"]]


# --- tiny TTL cache: catalog queries are slow on big schemas and rarely change ---
_cache: dict = {}
_cache_lock = threading.Lock()


def cached(key: tuple, produce):
    ttl = float(os.environ.get("SQL_MCP_CACHE_TTL", os.environ.get("MYSQL_CACHE_TTL", "60")))
    now = time.monotonic()
    if ttl > 0:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
    value = produce()
    if ttl > 0:
        with _cache_lock:
            _cache[key] = (now + ttl, value)
    return value
