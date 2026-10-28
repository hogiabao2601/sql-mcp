"""Masking by SOURCE column (REVIEW #4). A safety net, not access control: column-level GRANTs are.

Policy (ADR 0001, section 4, as refined in Phase 4):
  * an output column whose value is derived from a masked source column (directly, through any
    expression, a UNION branch, a CTE, a derived table or a scalar subquery) is returned as "***";
  * a masked column used anywhere else (WHERE, JOIN ... ON, GROUP BY, HAVING, ORDER BY, a subquery
    in a predicate) refuses the query: those positions leak values as a boolean oracle;
  * `reads_masked` tells the executor to hide the engine's error text if the query fails
    (e.g. EXTRACTVALUE() puts the value into the error message).
Columns are resolved with sqlglot's qualifier against the real catalog, so aliases cannot hide them.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope

from .guard import Checked
from .util import Refused


@dataclass
class MaskPlan:
    columns: set[int] = field(default_factory=set)   # output positions to mask
    reads_masked: bool = False                       # the query touches a masked column at all


def _is_masked(name: str, patterns) -> bool:
    return any(m in name.lower() for m in patterns)


def referenced_tables(c: Checked, default_db: str | None) -> set[tuple[str, str]]:
    """(database, table) pairs the statement reads, for the catalog lookup."""
    ctes = {cte.alias_or_name.lower() for cte in c.tree.find_all(exp.CTE)}
    out = set()
    for t in c.tree.find_all(exp.Table):
        if not t.name or (not t.db and t.name.lower() in ctes):
            continue
        db = t.db or default_db
        if db:
            out.add((db, t.name))
    return out


def plan(c: Checked, engine: str, default_db: str | None, schema: dict, patterns) -> MaskPlan:
    if not patterns or c.kind != "select":
        return MaskPlan()
    # Fast path (most queries): every table the query reads is known and none has a masked column.
    tables = referenced_tables(c, default_db)
    known = {(db.lower(), t.lower()): cols for db, ts in schema.items() for t, cols in ts.items()}
    if all((db.lower(), t.lower()) in known for db, t in tables) and not any(
            _is_masked(col, patterns) for db, t in tables for col in known[(db.lower(), t.lower())]):
        return MaskPlan()
    try:
        tree = qualify(c.tree.copy(), schema=schema, db=default_db, dialect=c.dialect,
                       validate_qualify_columns=True, identify=False)
        scopes = list(traverse_scope(tree))
    except SqlglotError:
        # Cannot resolve every column (unknown table/column, unusual syntax): fall back to the label and
        # original-name check in the executor, and hide error texts if anything looks secret.
        idents = {i.name for i in c.tree.find_all(exp.Identifier)}
        return MaskPlan(reads_masked=any(_is_masked(n, patterns) for n in idents))

    out: dict[int, list[bool]] = {}                 # id(scope expression) -> masked flag per output column
    by_name: dict[int, dict[str, bool]] = {}
    reads = False

    def col_masked(col: exp.Column, scope: Scope) -> bool:
        nonlocal reads
        src = scope.sources.get(col.table)
        if isinstance(src, exp.Table):
            hit = _is_masked(col.name, patterns)
            reads = reads or hit
            return hit
        if isinstance(src, Scope):
            return by_name.get(id(src.expression), {}).get(col.name.lower(), False)
        return False

    for scope in scopes:                             # children come before their parents
        e = scope.expression
        if isinstance(e, exp.SetOperation):
            left, right = out.get(id(e.left), []), out.get(id(e.right), [])
            flags = [a or b for a, b in zip(left, right, strict=False)]   # valid SQL: same width; never crash
            out[id(e)] = flags
            names = [s.alias_or_name.lower() for s in (e.left.selects if isinstance(e.left, exp.Select) else [])]
            by_name[id(e)] = dict(zip(names, flags, strict=False))
            continue
        if not isinstance(e, exp.Select):
            continue
        in_proj: dict[int, int] = {}                 # id(node) -> projection index
        for i, proj in enumerate(e.selects):
            for n in proj.walk():
                in_proj[id(n)] = i
        flags = [False] * len(e.selects)
        for col in scope.columns:
            hit = col_masked(col, scope)
            if not hit:
                continue
            if id(col) in in_proj:
                flags[in_proj[id(col)]] = True
            else:
                raise Refused(f"The masked column '{col.name}' cannot be used in WHERE/JOIN/GROUP BY/HAVING/"
                                "ORDER BY: that would reveal its values. Select it directly (it is returned as "
                                "***), or ask for a view or a column GRANT without it.")
        for sub in scope.subquery_scopes:            # scalar / IN / EXISTS subqueries of this scope
            if not any(out.get(id(sub.expression), [])):
                continue
            if id(sub.expression) in in_proj:
                flags[in_proj[id(sub.expression)]] = True
            else:
                raise Refused("A subquery that reads a masked column cannot be used in a predicate: that "
                                "would reveal its values.")
        out[id(e)] = flags
        by_name[id(e)] = {s.alias_or_name.lower(): f for s, f in zip(e.selects, flags, strict=True)}

    root = out.get(id(tree), [])
    return MaskPlan(columns={i for i, f in enumerate(root) if f}, reads_masked=reads)
