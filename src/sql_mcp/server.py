"""sql-mcp: a read-only MCP server for SQL databases (stdio).

SAFETY MODEL, in order of how much you can trust each layer:
  3. the database ACCOUNT you connect with (give it SELECT only)   <- the real security
  2. the session/transaction the ENGINE enforces: read-only + statement time limit
  1. the guard (guard.py)                                            <- a convenience for the model

RULE #1 of stdio servers: never print() to stdout. Log to stderr only (executor.log).
"""
from __future__ import annotations

import functools
import inspect
import json
import time
import uuid

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__, audit
from .config import Profile, config, get_profile
from .dialects import DIALECTS
from .executor import _pools, cached, engine_for, log, rows_of, run
from .guard import IDENT, check, check_ident, check_read_only, with_limit
from .masking import plan, referenced_tables
from .util import QueryTimeout, Refused, request_id
from .results import DEFAULT_ROWS, HARD_MAX_ROWS, MAX_CELL, MAX_CHARS, cell, dump, format_overview

# Tools only read, and only talk to the database you configured. These hints let the
# client treat them as safe (some clients skip permission prompts for read-only tools).
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)     # sent as readOnlyHint / openWorldHint


def resolve_db(p: Profile, database: str | None, required: bool = False, schema: str | None = None) -> str | None:
    """The database (MySQL family) or schema (PostgreSQL) a call works in. `schema` is an alias of `database`.
    Plain names pass as before; any other name must match one in the catalog exactly (REVIEW #12)."""
    if schema is not None:
        if database is not None and database != schema:
            raise ToolError("Pass either database or schema (they mean the same here), not two different values.")
        database = schema
    d = engine_for(p)[0]
    if database is not None and not IDENT.match(database):
        if d.find_database(_rows(p), database) is None:
            raise ToolError("Invalid database name: use letters, digits, _ and $ only, "
                            "or the exact name of an existing database.")
    db = database or d.default_namespace()
    if required and not db:
        raise ToolError(f"No database given and profile '{p.name}' has no default. Pass database=...")
    return db


def required_db(p: Profile, database: str | None, schema: str | None = None) -> str:
    db = resolve_db(p, database, required=True, schema=schema)
    assert db is not None          # resolve_db(required=True) raises instead of returning None
    return db


def _rows(p: Profile):
    return lambda sql, args=None, database=None: rows_of(p, sql, args, database)


def resolve_table(p: Profile, db: str, table: str) -> str:
    """Plain names pass as before (a missing table then reads 'not found'); any other name must match a
    table in the catalog exactly, and is quoted by the dialect wherever it is used in SQL (REVIEW #12)."""
    if IDENT.match(table) or engine_for(p)[0].find_table(_rows(p), db, table) is not None:
        return table
    return check_ident(table, "table")         # raises the legacy "Invalid table name" message


# --------------------------------------------------------------------------------------
# TOOLS. Every tool takes an optional `profile`. Errors that the model can fix are raised
# as ToolError (in mcp 2.x only ToolError text reaches the model; other exceptions are masked).
# --------------------------------------------------------------------------------------
def list_profiles() -> str:
    """List the configured database profiles (connections) and which one is the default.
    Call this first if you are unsure which database to query. Never shows passwords."""
    profiles, default = config()
    return dump({"default": default, "profiles": [
        {"name": n, "engine": p.engine, "host": p.host, "port": p.port, "user": p.user, "database": p.database,
         **({"schema": p.schema} if p.schema else {}),
         "engine_read_only": getattr(DIALECTS.get(p.engine), "engine_read_only", True),
         "timeout_ms": p.timeout_ms, "masked_columns": list(p.mask),
         "tls": p.tls not in ("disable", "prefer"), "tls_mode": p.tls, "tls_verified": p.tls.startswith("verify")}
        for n, p in profiles.items()]})


def list_databases(include_system: bool = False, profile: str | None = None) -> str:
    """List the databases visible to the connected user. System schemas are hidden unless include_system=true."""
    p = get_profile(profile)
    d = engine_for(p)[0]
    names = cached((p.name, "dbs"), lambda: d.databases(_rows(p)))
    return dump([n for n in names if include_system or not d.is_system(n)])


def list_tables(database: str | None = None, profile: str | None = None, schema: str | None = None) -> str:
    """List tables of a database with type, engine, APPROXIMATE row count, and size in MB.
    Use it to find the big tables before querying them."""
    p = get_profile(profile)
    db = required_db(p, database, schema)
    d = engine_for(p)[0]
    return cached((p.name, "tables", db), lambda: dump(d.fetch_tables(_rows(p), db)))


def describe_table(table: str, database: str | None = None, profile: str | None = None,
                   schema: str | None = None) -> str:
    """Describe one table: columns (type, nullability, key, default, comment), indexes,
    and foreign keys in BOTH directions (what it references, and what references it).
    Use the foreign keys to work out JOINs."""
    p = get_profile(profile)
    db = required_db(p, database, schema)
    table = resolve_table(p, db, table)

    def build() -> str:
        out = engine_for(p)[0].describe(_rows(p), db, table)
        if not out:
            raise ToolError(f"Table '{db}.{table}' not found (or no privilege). Try list_tables or search_schema.")
        return dump(out)
    return cached((p.name, "describe", db, table), build)


def search_schema(keyword: str, database: str | None = None, profile: str | None = None,
                  schema: str | None = None) -> str:
    """Find tables and columns whose NAME contains the keyword (case-insensitive).
    Searches the given/default database, or every non-system database if none is set. Max 100 hits each."""
    p = get_profile(profile)
    db = resolve_db(p, database, schema=schema)
    if not keyword.strip():
        raise ToolError("keyword is empty.")
    like = "%" + keyword.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return dump(engine_for(p)[0].search(_rows(p), like, db))


def sample_rows(table: str, limit: int = 5, database: str | None = None, profile: str | None = None,
                schema: str | None = None) -> str:
    """Return a few rows (1-20) from a table so you can see what the data looks like. Masked columns stay hidden."""
    p = get_profile(profile)
    db = required_db(p, database, schema)
    table = resolve_table(p, db, table)
    d = engine_for(p)[0]
    n = max(1, min(int(limit), 20))
    return dump(run(p, f"SELECT * FROM {d.quote(db)}.{d.quote(table)} LIMIT {n}", None, db, max_rows=n))


def execute_query(query: str, database: str | None = None, max_rows: int = DEFAULT_ROWS,
                  profile: str | None = None, schema: str | None = None) -> str:
    """Run ONE read-only statement (SELECT, WITH, SHOW, DESCRIBE, EXPLAIN). No comments, no semicolons.
    Returns {"columns":[...],"rows":[[...]]}; check the "truncated" field, and add a LIMIT or filters
    if it is present. Secret-looking columns are returned as "***" (see "masked")."""
    p = get_profile(profile)
    db = resolve_db(p, database, schema=schema)
    n = max(1, min(int(max_rows), HARD_MAX_ROWS))
    audit.note_statement(query, None)
    checked = check(query, p.engine)
    audit.note_statement(query, checked)
    m = _mask_plan(p, checked, db)
    sql, capped = with_limit(checked, n + 1)
    return dump(run(p, sql, None, db, max_rows=n, capped=capped, stream=not capped,
                    mask_columns=frozenset(m.columns), hide_errors=m.reads_masked))


def _mask_plan(p: Profile, checked, db: str | None):
    """Which output columns derive from masked source columns (looks up only the tables the query reads)."""
    tables = referenced_tables(checked, db) if p.mask and checked.kind == "select" else set()
    d = engine_for(p)[0]
    schema = cached((p.name, "columns", frozenset(tables)), lambda: d.columns(_rows(p), tables)) if tables else {}
    return plan(checked, p.engine, db, schema, p.mask)


def explain_query(query: str, database: str | None = None, profile: str | None = None,
                  schema: str | None = None) -> str:
    """Show the database's execution plan for a SELECT without running it. On MySQL look at 'type'
    (ALL = full scan), 'key' (index used), and 'rows' (estimated rows) to judge whether a query is slow."""
    p = get_profile(profile)
    db = resolve_db(p, database, schema=schema)
    audit.note_statement(query, None)
    checked = check(query, p.engine)
    audit.note_statement(query, checked)
    if checked.kind != "select":
        raise ToolError("explain_query takes a SELECT (or WITH ... SELECT).")
    return dump(run(p, engine_for(p)[0].explain_prefix() + checked.text, None, db, max_rows=100, mask=False))


# RESOURCE: the whole schema as compact text the app can attach as context in one go.
def schema_overview(profile: str, database: str) -> str:
    """Compact overview of every table and column in a database, with foreign keys.
    Capped at 30,000 characters; a last line says how many tables were left out."""
    p = get_profile(profile)
    db = required_db(p, database)
    return cached((p.name, "overview", db), lambda: format_overview(engine_for(p)[0].overview(_rows(p), db)))


# PROMPTS: reusable starting points the user picks (slash commands in Claude Code)
def _prompts(label: str):
    def explore_database(database: str = "") -> str:
        """Get oriented in a database: tables, sizes, relationships."""
        target = f"the database '{database}'" if database else "the default database"
        return (f"Explore {target} using the {label} tools. First call list_profiles, then list_tables, then "
                "describe_table for the 3 largest tables. Summarize what the data model represents, how the "
                "tables relate (use the foreign keys), and anything that looks odd. Do not guess: only report "
                "what the tools returned.")

    def investigate(question: str) -> str:
        """Answer a data question with read-only SQL, showing the SQL used."""
        return (f"Answer this question using the {label} tools only:\n\n" + question + "\n\n"
                "Steps: (1) search_schema / describe_table to find the right tables and join keys, "
                "(2) write one read-only query, run explain_query first if the tables are large, "
                "(3) run it with execute_query, (4) give the answer and show the exact SQL. "
                "If the result says truncated, refine the query instead of reporting partial numbers as totals.")
    return explore_database, investigate


TOOLS = (list_profiles, list_databases, list_tables, describe_table, search_schema, sample_rows,
         execute_query, explain_query)


def _finish(event: dict, fn, args: dict, outcome: str, reason: str | None, out) -> None:
    """Fill in the audit event. Values the caller typed (keywords, error texts) are not recorded by default."""
    event["ms"] = round((time.monotonic() - event.pop("_t0")) * 1000)
    event["outcome"] = outcome
    try:
        name = args.get("profile") or config()[1]
        event.update(profile=name, engine=get_profile(name).engine)
    except Exception:                     # config errors are the call's outcome already
        event["profile"] = args.get("profile")
    for key in ("database", "schema", "table"):
        if args.get(key):
            event[key] = args[key]
    if args.get("keyword"):
        event["keyword"] = args["keyword"] if audit.literals() else f"<{len(args['keyword'])} chars>"
    if reason:
        event["reason"] = reason
    if isinstance(out, str) and out.startswith("{"):
        try:
            body = json.loads(out)
            if "row_count" in body:
                event["rows"] = body["row_count"]
                if body.get("truncated"):
                    event["truncated"] = body["truncated"]
        except ValueError:
            pass


def audited(fn):
    """Give every call a request id (in stderr logs too) and, when SQL_MCP_AUDIT_LOG is set, one audit line."""
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        rid = uuid.uuid4().hex[:12]
        rid_token = request_id.set(rid)
        event = audit.new_event(fn.__name__, rid) if audit.enabled() else None
        ev_token = audit.current.set(event)
        outcome, reason, out = "ok", None, None
        try:
            out = fn(*args, **kwargs)
            return out
        except Refused as e:
            outcome, reason = "refused", str(e)
            raise
        except QueryTimeout:
            outcome = "timeout"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            if event is not None:
                try:
                    _finish(event, fn, dict(sig.bind_partial(*args, **kwargs).arguments), outcome, reason, out)
                    audit.record(event)
                except Exception as e:          # auditing must never break a call
                    log(f"audit failed: {type(e).__name__}: {e}")
            audit.current.reset(ev_token)
            request_id.reset(rid_token)
    return wrapper


_AUDITED = {f.__name__: audited(f) for f in (*TOOLS, schema_overview)}


def audited_tools() -> dict:
    return _AUDITED


def build_server(name: str = "sql-mcp", version: str = __version__, label: str = "sql") -> MCPServer:
    mcp = MCPServer(name, version=version)
    for fn in TOOLS:
        # structured_output=False: results are JSON text already. The SDK's default would wrap that text a second
        # time as structuredContent {"result": "<escaped JSON>"}: 2,563 instead of 1,304 characters for a 40-row
        # result, with the same content for the model (REVIEW #13).
        mcp.tool(annotations=READ_ONLY, structured_output=False)(_AUDITED[fn.__name__])
    mcp.resource("schema://{profile}/{database}", mime_type="text/plain")(_AUDITED["schema_overview"])
    for fn in _prompts(label):
        mcp.prompt()(fn)
    return mcp


def main() -> None:
    log(f"sql-mcp {__version__} starting on stdio")
    build_server().run(transport="stdio")


__all__ = ["DEFAULT_ROWS", "HARD_MAX_ROWS", "MAX_CELL", "MAX_CHARS", "Profile", "TOOLS", "build_server", "cached",
           "cell", "check_ident", "check_read_only", "config", "dump", "engine_for", "get_profile", "log", "main",
           "rows_of", "run", "_pools", *(f.__name__ for f in TOOLS), "schema_overview"]
