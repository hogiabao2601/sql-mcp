"""Result shaping: compact JSON, small cells, and the row/size caps that protect the model's context."""
from __future__ import annotations

import json

DEFAULT_ROWS = 100       # rows returned by execute_query unless the caller asks for more
HARD_MAX_ROWS = 1000     # ...and the most it can ever ask for
MAX_CHARS = 30_000       # cap on serialized result size; protects the model's context window
MAX_CELL = 500           # long TEXT/JSON cells are cut to this many characters
SCHEMA_MAX_CHARS = 30_000          # cap on the schema:// resource text (REVIEW #5)
INTERNAL_MAX_ROWS = 20_000         # our own catalog reads: far above any normal schema, and never cut silently
INTERNAL_MAX_CHARS = 5_000_000


def dump(obj) -> str:
    """Compact JSON (no spaces) = fewer tokens for the model. default=str handles Decimal/datetime."""
    return json.dumps(obj, separators=(",", ":"), default=str, ensure_ascii=False)


def cell(value):
    """Make one value safe and small for the model."""
    if isinstance(value, (bytes, bytearray)):
        return f"<binary {len(value)} bytes>"
    if isinstance(value, (dict, list)):          # JSON / arrays (PostgreSQL returns Python objects)
        text = dump(value)
        return value if len(text) <= MAX_CELL else f"{text[:MAX_CELL]}...(+{len(text) - MAX_CELL} chars)"
    if isinstance(value, str) and len(value) > MAX_CELL:
        return f"{value[:MAX_CELL]}...(+{len(value) - MAX_CELL} chars)"
    return value


def collect(fetchmany, masked: set[int], max_rows: int, max_chars: int = MAX_CHARS) -> tuple[list[list], str | None]:
    """Read rows in batches until a cap is hit. Returns (rows, truncated_reason_or_None).
    'truncated' is set only when a row beyond the cap actually exists."""
    rows: list[list] = []
    size = 0
    while True:
        batch = fetchmany(200)
        if not batch:
            return rows, None
        for raw in batch:
            if len(rows) >= max_rows:
                return rows, f"row limit {max_rows}"
            row = [("***" if i in masked and v is not None else cell(v)) for i, v in enumerate(raw)]
            size += len(dump(row))
            if size > max_chars:
                return rows, f"size limit {max_chars} chars"
            rows.append(row)


def format_overview(tables: dict[str, list[str]], max_chars: int = SCHEMA_MAX_CHARS) -> str:
    """One line per table, "name(col type tags, ...)". Cut at a table boundary when the text would pass
    max_chars, and then say how many tables are missing (never a silent cut)."""
    lines: list[str] = []
    size = 0
    for t, cols in tables.items():
        line = f"{t}({', '.join(cols)})"
        if size + len(line) + (1 if lines else 0) > max_chars:
            missing = len(tables) - len(lines)
            lines.append(f"... {missing} more tables not shown (schema larger than {max_chars} chars); "
                         "use list_tables / describe_table / search_schema.")
            break
        lines.append(line)
        size += len(line) + (1 if len(lines) > 1 else 0)
    return "\n".join(lines)
