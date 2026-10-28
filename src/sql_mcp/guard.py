"""Layer 1: a convenience that gives the model fast, readable refusals. NOT a security boundary:
the engine's read-only session (layer 2) and the database account (layer 3) are.

Each statement is parsed with sqlglot for the profile's dialect (ADR 0001, section 4). Anything that
does not parse is refused (fail closed). Accepted: one SELECT / set operation; on the MySQL family
also SHOW (without subqueries) and DESCRIBE/EXPLAIN of a table or a SELECT (never ANALYZE).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NoReturn

import sqlglot
from mcp.server.mcpserver.exceptions import ToolError
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

from .util import Refused

SQLGLOT_DIALECT = {"mysql": "mysql", "mariadb": "mysql", "postgres": "postgres", "sqlite": "sqlite",
                   "starrocks": "starrocks"}
MYSQL_FAMILY = {"mysql", "mariadb", "starrocks"}

DENY_FUNCS = {
    "mysql": {"sleep", "benchmark", "get_lock", "release_lock", "release_all_locks", "is_free_lock", "is_used_lock",
              "load_file", "master_pos_wait", "source_pos_wait", "wait_for_executed_gtid_set",
              "wait_until_sql_thread_after_gtids"},
    "postgres": {"pg_sleep", "pg_sleep_for", "pg_sleep_until", "set_config", "pg_advisory_lock",
                 "pg_advisory_lock_shared", "pg_advisory_xact_lock", "pg_try_advisory_lock", "pg_read_file",
                 "pg_read_binary_file", "pg_ls_dir", "pg_stat_file", "lo_import", "lo_export", "dblink",
                 "dblink_exec", "dblink_connect", "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf",
                 "pg_notify", "pg_ls_logdir", "pg_ls_waldir", "pg_ls_tmpdir", "pg_ls_archive_statusdir",
                 "pg_read_server_files", "pg_file_write", "lo_create", "lo_creat", "lo_unlink", "lo_put", "lo_get",
                 "lo_from_bytea", "lo_open", "lowrite", "loread", "lo_truncate", "pg_logical_emit_message",
                 "pg_switch_wal", "pg_create_restore_point", "txid_current", "pg_current_xact_id"},
    "sqlite": {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"},
    # StarRocks FILES(): reads any file / S3 / HDFS location, with credentials given in the statement itself
    "starrocks": {"sleep", "files"},
}
DENY_FUNCS["mariadb"] = DENY_FUNCS["mysql"] | {"master_gtid_wait"}

# Any of these anywhere in the tree means the statement writes, locks, exports, or changes the session.
REFUSED_NODES = tuple(n for n in (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter, exp.Command, exp.Into,
    exp.Lock, exp.Set, exp.Transaction, exp.Commit, exp.Rollback, exp.Use, exp.Copy, exp.Pragma,
    getattr(exp, "Attach", None), getattr(exp, "Detach", None), exp.Cache, exp.Uncache, exp.LoadData,
    getattr(exp, "Grant", None), getattr(exp, "Revoke", None), exp.Hint) if isinstance(n, type))

IDENT = re.compile(r"^[A-Za-z0-9_$]{1,64}$")          # database / table names (strict legacy rule)


@dataclass(frozen=True)
class Checked:
    text: str            # the caller's statement, trimmed and without a trailing ';'
    tree: exp.Expr
    kind: str            # "select" | "show" | "describe"
    dialect: str         # sqlglot dialect name


def _refuse(msg: str) -> NoReturn:
    raise Refused(msg)


def _func_name(f: exp.Func) -> str:
    return (f.name if isinstance(f, exp.Anonymous) else f.sql_name()).lower()


def _check_tree(tree: exp.Expr, engine: str) -> None:
    deny = DENY_FUNCS.get(engine, set())
    for node in tree.walk():
        if isinstance(node, exp.Hint):
            _refuse("Optimizer hints are not allowed (they can override the server's time limit).")
        if isinstance(node, exp.Lock):
            _refuse("Locking reads (FOR UPDATE / FOR SHARE / LOCK IN SHARE MODE) are not allowed.")
        if isinstance(node, exp.Into):
            _refuse("SELECT ... INTO is not allowed: it writes a table, a file, or variables.")
        if isinstance(node, REFUSED_NODES):
            _refuse(f"Statement contains a {type(node).__name__} clause; only reads are allowed.")
        if isinstance(node, exp.Func) and _func_name(node) in deny:
            _refuse(f"The function {_func_name(node)}() is not allowed (it sleeps, locks, reads files, "
                    "or changes the session).")


def check(sql: str, engine: str = "mysql") -> Checked:
    """Parse and vet one statement for `engine` (a profile engine name). Raises ToolError with a reason."""
    text = sql.strip().rstrip(";").strip()
    if not text:
        _refuse("Empty query.")
    dialect = SQLGLOT_DIALECT.get(engine, engine)
    try:
        tokens = sqlglot.Dialect.get_or_raise(dialect).tokenize(text)
        if any(t.token_type == TokenType.HINT for t in tokens):
            _refuse("Optimizer hints are not allowed (they can override the server's time limit).")
        if any(t.comments for t in tokens):
            _refuse("SQL comments are not allowed.")       # also MySQL /*! executable comments */, which the AST drops
        statements = [s for s in sqlglot.parse(text, read=dialect) if s is not None]
    except SqlglotError as e:
        _refuse(f"Could not parse this as {engine} SQL ({str(e).splitlines()[0][:120]}). "
                "Send one plain SELECT (or WITH ... SELECT).")
    if len(statements) != 1:
        _refuse("Only one statement per call is allowed.")
    tree = statements[0]

    if isinstance(tree, (exp.Select, exp.SetOperation, exp.Subquery)):
        _check_tree(tree, engine)
        return Checked(text, tree, "select", dialect)
    if engine in MYSQL_FAMILY and isinstance(tree, exp.Show):
        if any(isinstance(n, (exp.Select, exp.Subquery)) for n in tree.walk() if n is not tree):
            _refuse("SHOW ... WHERE with a subquery is not allowed (it escapes the server's time limit). "
                    "Query information_schema with SELECT instead.")
        _check_tree(tree, engine)
        return Checked(text, tree, "show", dialect)
    if engine in MYSQL_FAMILY and isinstance(tree, exp.Describe):
        if tree.args.get("style"):
            _refuse("EXPLAIN/DESCRIBE ANALYZE executes the query. Use explain_query instead.")
        target = tree.this
        if not isinstance(target, (exp.Table, exp.Select, exp.SetOperation, exp.Subquery)):
            _refuse("DESCRIBE/EXPLAIN takes a table name or a SELECT.")
        _check_tree(target, engine)
        return Checked(text, tree, "describe", dialect)
    allowed = "SELECT, WITH, SHOW, DESCRIBE and EXPLAIN" if engine in MYSQL_FAMILY else "SELECT and WITH ... SELECT"
    _refuse(f"Only {allowed} are allowed, got {type(tree).__name__.upper()}.")


def with_limit(c: Checked, n: int) -> tuple[str, bool]:
    """(sql, capped): the statement limited to `n` rows so the server stops producing rows at the cap
    (no drain, no reconnect: REVIEW #6), and whether that is guaranteed.

    Only ever APPENDS to the caller's own text; it never regenerates SQL. (A regenerated
    "OFFSET 2 ROWS FETCH FIRST 3 ROWS ONLY" came back as "LIMIT 101" and returned 38 rows instead of 3.)
    With any other LIMIT / OFFSET / FETCH the text is left alone; the executor's row cap still applies."""
    if c.kind != "select":
        return c.text, False
    limit, offset = c.tree.args.get("limit"), c.tree.args.get("offset")
    if limit is None and offset is None:
        return f"{c.text} LIMIT {n}", True
    value = limit.expression if isinstance(limit, exp.Limit) else None
    small = isinstance(value, exp.Literal) and value.is_int and int(value.name) <= n
    return c.text, small


def check_read_only(sql: str, engine: str = "mysql") -> str:
    """Legacy API (mysql_server.py v2): vet the statement and return it trimmed."""
    return check(sql, engine).text


def check_ident(value: str, what: str) -> str:
    if not IDENT.match(value):
        raise ToolError(f"Invalid {what} name: use letters, digits, _ and $ only.")
    return value
