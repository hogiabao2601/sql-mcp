# sql-mcp

A **read-only** [Model Context Protocol](https://modelcontextprotocol.io) server that lets an AI assistant
(Claude Code, Claude Desktop, any MCP client) explore and query SQL databases safely, over stdio.

One server, five engines, one tool contract: **MySQL, MariaDB, PostgreSQL, SQLite, StarRocks**. Every engine
passes the same conformance suite before it is supported.

- Eight read-only tools (list/describe/search schema, sample rows, run a query, see a query plan), plus a
  `schema://` resource and two prompts.
- Three layers of protection, in order of trust: a least-privilege **database account**, an engine-enforced
  **read-only session + statement timeout**, and a **SQL guard** that parses every statement.
- Secret-looking columns are returned as `***` (masking by source column, not just by label).
- Results are compact JSON, capped in rows, bytes and cell size, streamed so a runaway query never buys memory.

> Status: works and is tested (see [Testing](#testing)). Versioned `0.1.0`. The code lives in the `sql_mcp`
> package; `mysql_server.py` is kept as a compatibility entry point for the original MySQL-only server.

## Supported databases

| Engine | Tested versions | Read-only enforcement (layer 2) | Statement timeout |
|---|---|---|---|
| MySQL | 8.4 | `SET SESSION transaction_read_only=1` on every connect | `max_execution_time` (SELECT only; a client deadline + `KILL QUERY` covers the rest) |
| MariaDB | 10.11, 11.4, 11.8 | `transaction_read_only=1` (11.1+) / `tx_read_only=1` (older), chosen from the server version | `max_statement_time` (all statements) |
| PostgreSQL | 14, 17, 18 | every statement in its own `BEGIN READ ONLY … ROLLBACK`, plus `default_transaction_read_only=on` | `statement_timeout` (all statements) |
| SQLite | 3.x (Python's built-in) | file opened `mode=ro` + `PRAGMA query_only=ON` + an authorizer allowlist | a progress-handler deadline |
| StarRocks | 4.1.1, 3.5.12 | **none — see the warning below** | `query_timeout` (whole seconds) |

Needs **Python 3.11+**. Install the PostgreSQL driver only if you use it: `pip install 'sql-mcp[postgres]'`.

## Safety model

Three layers, from most to least trusted. **Layer 1 is a convenience, never the security boundary.**

1. **The database account.** Connect with an account that has `SELECT` only (and column-level grants for
   secrets). The server cannot exceed what the account is allowed to do. *This is the real control.*
2. **The engine-enforced session.** A read-only session/transaction and a statement timeout, applied on every
   connect so a reconnect cannot forget them (per-engine, see the table above).
3. **The guard.** Every statement is parsed with [sqlglot](https://github.com/tobymao/sqlglot) for the
   engine's dialect. Only a single `SELECT` / `WITH … SELECT` is allowed (plus `SHOW`/`DESCRIBE`/`EXPLAIN` on
   MySQL-family engines). Comments, optimizer hints, locking reads, `INTO`, multiple statements, and functions
   that sleep/lock/read files are refused. Anything that does not parse is refused (fail closed).

## Quick start

```bash
git clone https://github.com/hogiabao2601/sql-mcp.git && cd sql-mcp
python3 -m venv .venv
.venv/bin/python -m pip install -e .            # add '.[postgres]' for PostgreSQL profiles
```

### The easy way: the setup wizard

```bash
.venv/bin/sql-mcp-setup        # opens a local browser UI (127.0.0.1 only, no extra dependencies)
```

Pick an engine, fill in the connection, click **Test connection** (it validates through the same loader and
dialect the server uses), then **Save profile**. The wizard writes `connections.json` and prints the exact
`claude mcp add` command (and an MCP config JSON for `--mcp-config` / Claude Desktop). The password you type is
used only for the test — the file stores a `password_env` / `password_cmd` reference, never the secret.

### Or by hand

Create a least-privilege account in your database, for example MySQL:

```sql
CREATE USER 'claude_ro'@'%' IDENTIFIED BY '<strong password>';
GRANT SELECT ON myproject.* TO 'claude_ro'@'%';
-- hide a column entirely instead of relying on masking:
--   GRANT SELECT (id, created_at) ON myproject.users TO 'claude_ro'@'%';
```

Write a profiles file (passwords are never stored in it — only the name of an env var, or a command to fetch
the secret):

```jsonc
// connections.json  (git-ignored)
{
  "default": "myproject",
  "profiles": {
    "myproject": {
      "engine": "mysql",
      "host": "127.0.0.1", "port": 3306,
      "user": "claude_ro",
      "password_env": "MYPROJECT_DB_PASSWORD",
      "database": "myproject",
      "mask_columns": ["password", "token", "secret", "ssn", "email"]
    }
  }
}
```

Register it with Claude Code (local scope keeps the secret in `~/.claude.json`, not in a committed file):

```bash
claude mcp add --scope local \
  --env SQL_MCP_CONFIG="$PWD/connections.json" --env MYPROJECT_DB_PASSWORD='<password>' \
  sql -- "$PWD/.venv/bin/sql-mcp"
```

Then ask: *"which databases can you see?"*, *"describe the orders table and what it links to"*,
*"how many orders were placed yesterday? show the SQL"*.

To try it with no real database, use the bundled Docker test stack — see [Testing](#testing).

## Configuration

A profile is one named connection. Profiles come from a JSON file (`SQL_MCP_CONFIG`), or a single MySQL
connection from `MYSQL_*` env vars. The file is re-read when it changes, so edits apply without a restart.

**Keys** (unknown keys and out-of-range values are refused at load):

| Key | Applies to | Meaning |
|---|---|---|
| `engine` | all | `mysql` (default), `mariadb`, `postgres`, `sqlite`, `starrocks` |
| `host`, `port` | networked engines | defaults per engine (3306 / 5432 / 9030) |
| `user` | networked engines | the database account |
| `database` | all | default database (MySQL family) / connection database (PostgreSQL) / one SQLite file name |
| `schema` | PostgreSQL | default schema (the tools' `database` argument means schema here; `schema=` is an alias) |
| `catalog` | StarRocks | the internal/external catalog this profile reads (default `default_catalog`) |
| `databases` / `path` | SQLite | a `{name: file}` map (up to 9), or a single file as database `main` |
| `timeout_ms` | all | statement time limit, 100–600000 (default 5000); `0` is refused |
| `pool_size` | networked engines | 1–20 (default 3) |
| `mask_columns` | all | column-name fragments whose values are returned as `***` (default: password/token/secret/…) |
| `tls`, `tls_ca` | networked engines | TLS mode and CA file (see below) |

**Password** — give exactly one of:
- `password_cmd`: an argv list run without a shell, e.g. `["security","find-generic-password","-s","mydb","-w"]`
  (its stdout is the password; a failure reports the exit code and stderr, never stdout);
- `password_env`: the name of an environment variable holding the password;
- `password`: a literal (works, but prefer the above).

**TLS** — `tls` (the older `ssl_ca` / `ssl_check_hostname` keys still work and map to `verify-full` / `verify-ca`):

| `tls` | Encrypted | Server verified | No TLS on server |
|---|---|---|---|
| `disable` | no | no | plaintext |
| `prefer` (default) | if offered | **no** | silent plaintext |
| `require` | yes | **no** | refused |
| `verify-ca` | yes | chain, against `tls_ca` | refused |
| `verify-full` | yes | chain + host name | refused |

`verify-*` need `tls_ca`. `prefer`/`require` log a warning on first connect; `list_profiles` reports
`tls_mode` and `tls_verified`. On PostgreSQL these map 1:1 to libpq `sslmode`/`sslrootcert`.

## Environment variables

| Variable | Purpose |
|---|---|
| `SQL_MCP_CONFIG` | path to the profiles JSON file (`MYSQL_CONFIG` also works) |
| `SQL_MCP_CACHE_TTL` | schema-metadata cache, seconds (default 60, `0` = off); `MYSQL_CACHE_TTL` also works |
| `SQL_MCP_LOG_QUERIES=1` | log SQL text to stderr (off by default); `MYSQL_LOG_QUERIES` also works |
| `SQL_MCP_AUDIT_LOG` | append one JSON line per tool call to this file (off by default; file created `0600`) |
| `SQL_MCP_AUDIT_LITERALS=1` | include literal values in the audit log (off by default — values are never written otherwise) |
| `MYSQL_HOST`/`PORT`/`USER`/`PASSWORD`/`DATABASE` | single-connection mode, no config file (MySQL only) |

**Audit log**: each line has a request id, tool, profile, engine, outcome (`ok`/`refused`/`error`/`timeout`),
row count, and the statement as a *fingerprint* (every literal replaced by `?`) with a hash. Literal values
and engine error texts are never written unless `SQL_MCP_AUDIT_LITERALS=1`. The same request id prefixes the
stderr log lines for that call.

## Tools, resource, prompts

| Tool | What it does |
|---|---|
| `list_profiles` | the configured connections and the default one (never shows passwords) |
| `list_databases` | databases/schemas visible to the account; system schemas hidden unless asked |
| `list_tables` | tables with type, engine, approximate rows and size (where the engine reports them) |
| `describe_table` | columns, indexes, and foreign keys both ways (what it references, what references it) |
| `search_schema` | find tables/columns whose name contains a keyword |
| `sample_rows` | 1–20 rows so the model can see the shape of the data (masked columns stay hidden) |
| `execute_query` | run one read-only statement; row/size caps; `database`/`schema`, `max_rows`, `profile` options |
| `explain_query` | the execution plan of a SELECT, without running it |

Also a resource template `schema://{profile}/{database}` (the whole schema as compact text, capped at 30,000
chars with a "N more tables not shown" marker) and two prompts, `explore_database` and `investigate`.

## Usage notes

- **Masking is a safety net, not access control.** A column whose *source* matches `mask_columns` comes back as
  `***` even through an alias, expression, `UNION`, CTE, derived table or scalar subquery; using a masked
  column in `WHERE`/`JOIN`/`GROUP BY`/`ORDER BY` is refused (it would leak values as a yes/no oracle); and the
  engine's error text is hidden when a failing query touched a masked column. It still matches by **name**
  (`ssn` also matches `classname`). For real protection use column-level `GRANT`s or views.
- **PostgreSQL**: a profile connects to one database; the tools' `database` argument selects a **schema**
  (`schema=` is an alias). Each call is a short read-only transaction (three round trips).
- **SQLite** has **no accounts** — layer 3 is the file's OS permissions. Make the database files read-only for
  the user that runs the server (`chmod 444`). A profile names its files; cross-file joins work.
- **The guard refuses some legal reads**: `TABLE t`, `VALUES (...)`, any SQL comment, PostgreSQL `SHOW`, SQLite
  `PRAGMA x` (use `SELECT * FROM pragma_table_info('t')`), and anything sqlglot cannot parse. Rewrite as a
  plain `SELECT`.

## ⚠️ StarRocks: read this first

StarRocks is supported but **weaker than the other engines**, and the README says so because the layer model
does not fully hold there:

- **No read-only mode.** `transaction_read_only` is accepted and ignored; there is no read-only transaction.
  Only the account (layer 3) and the guard (layer 1) stop a write.
- **A SELECT-only account can still export files.** `INSERT INTO FILES(...)`, `SELECT … INTO OUTFILE`, and the
  `FILES()` table function reach any file/S3/HDFS location, with credentials supplied **inside the SQL**, so
  removing credentials from the servers is not enough. The guard refuses all of these (including `FILES()`
  inside a `SELECT`), but that makes the guard a security control for StarRocks.
- Users can raise their own `query_timeout` (`SET`, `SET_VAR` hints); the guard refuses those and a client
  deadline (`KILL QUERY`) is the backstop.

If you use StarRocks: connect with a SELECT-only account on the narrowest set of databases, and **block file
export at the infrastructure level** (no storage credentials on the backends *and* network egress rules —
credentials-in-SQL means egress policy is the only reliable barrier). External catalogs (Iceberg, Hive) are
supported read-only via a `catalog` key; masking resolves external columns too.

## Testing

A Docker test stack with seeded `shop`/`analytics` data ships in the repo. Full instructions are in
[`docs/TESTING.md`](docs/TESTING.md). The short version:

```bash
.venv/bin/python -m pip install -e '.[postgres,dev]'
.venv/bin/python -m unittest discover -s tests          # offline: guard, masking, config, pool, results, audit
.venv/bin/ruff check src tests bench mysql_server.py && .venv/bin/mypy

export PATH="$HOME/.docker/bin:$PATH"   # or wherever your docker is
docker compose up -d --wait                             # MySQL 8.4 test DB on 127.0.0.1:33316
.venv/bin/python tests/smoke_mysql.py                   # 56 end-to-end checks over real stdio JSON-RPC
SQL_MCP_ENGINE=mysql .venv/bin/python -m unittest tests.conformance.conformance_suite -v
```

The conformance suite (`tests/conformance/`) runs the same checks against every engine, selected by
`SQL_MCP_ENGINE`; `docs/TESTING.md` lists the compose files and ports for MariaDB, PostgreSQL, SQLite and
StarRocks (including external catalogs and TLS).

## Design and review

- `docs/adr/0001-multi-database.md` — the architecture decision record (engine abstraction, guard, config,
  layout), with the spike results it rests on.
- `REVIEW.md` — a skeptical review of the original MySQL server, with each finding's fix tracked.

## License

MIT — see [LICENSE](LICENSE).
