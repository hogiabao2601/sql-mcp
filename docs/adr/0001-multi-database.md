# ADR 0001: One read-only MCP server for many SQL engines

- Status: **Accepted** (owner decisions in section 9 and section 2.4). Implementation: MySQL (P0) and MariaDB, PostgreSQL, SQLite, StarRocks (P1) done and passing `tests/conformance/` on MySQL 8.4; MariaDB 10.11, 11.4, 11.8; PostgreSQL 14, 17, 18; SQLite 3.54; StarRocks 4.1.1, 3.5.12. StarRocks' two gaps (section 2.4) are asserted by tests that fail if StarRocks ever closes them. P2 (operability) done: `password_cmd`, opt-in JSON audit log (no literals), structured output off, profile hot-reload, ruff + mypy clean. External catalogs: StarRocks gains a `catalog` key; Iceberg (REST + S3) is tested through `tests/conformance/starrocks_external.py` incl. masking of external aliased secret columns; the Hive code path is identical and the metastore is wired up, but live Hive data creation is blocked by a SeaweedFS (the MinIO replacement) S3A mkdir quirk, so that test skips. Shared-data StarRocks is protocol-identical to the tested allin1 cluster at the MySQL layer, so it is documented (compose) rather than stood up, to spare memory. A real Claude Code session (headless, `--mcp-config --strict-mcp-config`, nothing registered) was verified: it queried through the tools and masking held.
- Date: 2026-10-27
- Evidence:
  - spike code and outputs in `spikes/` (`bench.py`, `adapters_raw.py`, `adapters_sa.py`, `spike_sqlglot.py`, `spike_sqlglot_corpus.py`, `out-*.txt`);
  - official-doc checks with URLs and verbatim quotes in `spikes/doc-verification.md` (cited below as "doc #n");
  - review findings in `REVIEW.md` (cited as "R#n").
- Test environment:
  - engines: MySQL 8.4.13, MariaDB 11.4.13, PostgreSQL 17.11, StarRocks 4.1.1 (all in Docker on 127.0.0.1), SQLite from Python 3.15.0;
  - libraries: PyMySQL 1.2.3, psycopg 3.3.6, SQLAlchemy 2.1.6, sqlglot 30.25.0.
  - Note that pip resolved SQLAlchemy **2.1**, not 2.0. The 2.0 docs were used for doc #22–24.

## 1. Context

`mysql_server.py` v2 is a MySQL-only, single-file server: 8 tools, 1 resource template and 2 prompts. The goal is one server for many SQL engines, with the same tool contract, and with MySQL profiles behaving exactly as today. The safety model must survive generalisation:

| Layer | Trust | What it is |
|---|---|---|
| 3 | the real boundary | a least-privilege database account |
| 2 | enforced by the engine, per session or transaction | read-only mode + statement timeout |
| 1 | convenience for the model | a guard that refuses non-read statements with a readable message |

Layer 1 is never presented as security.

## 2. What the spikes established (facts the design rests on)

### 2.1 Layer 2 per engine (measured, writer account, no guard)

| Engine | Read-only mechanism that held | Write attempt result | Timeout mechanism | Heavy query at 1,000 ms |
|---|---|---|---|---|
| MySQL 8.4 | `SET SESSION transaction_read_only=1` | `1792 Cannot execute statement in a READ ONLY transaction` (also for `CREATE TEMPORARY TABLE`) | `max_execution_time` (ms) | killed at 1.02 s, error 3024 |
| MariaDB 11.4 | `SET SESSION transaction_read_only=1` (11.1+; `tx_read_only` before, doc #5) | `1792` (also temp tables) | `max_statement_time` (**seconds**, fractional; doc #6) | killed at 1.02 s, error 1969 |
| PostgreSQL 17 | every statement in `BEGIN READ ONLY ... ROLLBACK` (psycopg `conn.read_only=True`, non-autocommit) | `ReadOnlySqlTransaction: cannot execute DELETE in a read-only transaction` | `statement_timeout` (ms) | killed at 1.05 s, `QueryCanceled` |
| SQLite | `mode=ro` URI + `PRAGMA query_only=ON` + an **authorizer** (deny writes, ATTACH, DETACH, all but read-only pragmas) | `not authorized`; `ATTACH`, `VACUUM INTO`, `load_extension`, `PRAGMA query_only=OFF` all refused, no file created | progress handler with a deadline | aborted at 1.00 s, `interrupted` |
| StarRocks 4.1 | **none**. `transaction_read_only=1` is accepted but not enforced; `START TRANSACTION READ ONLY` is a syntax error (doc S1). | the writer's **INSERT succeeded** under `transaction_read_only=1` | `query_timeout` (seconds, 1 to 259200; covers SELECT, SHOW and `SLEEP`; doc S3) | killed at 1.05 s, error 5024 |

**Gaps proven in the spikes, which the design must close:**

- **MySQL: `max_execution_time` covers SELECT only.**
  - `SHOW ... WHERE (subquery)` ran past it: 2.5 s under a 1 s limit, and 30 s plus two orphan queries through the server (R#3).
  - An optimizer hint `/*+ MAX_EXECUTION_TIME(60000) */` sent by `readonly_user` overrode a 1 s session limit (2.5 s, completed).
  - MariaDB's `max_statement_time` covers all statements (doc #6): `SHOW ... WHERE (subquery)` was killed at 1.02 s.
- **PostgreSQL: a SELECT can turn layer 2 off in autocommit mode.**
  - `SELECT set_config('default_transaction_read_only','off',false)` followed by `DELETE` **succeeded** (rowcount 0, as the writer account).
  - With the per-statement `BEGIN READ ONLY ... ROLLBACK`, the same sequence was refused, because PostgreSQL rolls back GUC changes with the transaction.
  - Changing `transaction_read_only` inside the transaction fails with `transaction read-write mode must be set before any query`. The docs do not state this (doc #8, "could not verify"); the spike does.
- **MariaDB: `init_command` with `max_execution_time` fails every connection** with `1193 Unknown system variable` (R#20 confirmed).
- **MySQL: the documentation suggests locking reads are refused in READ ONLY transactions** (doc #3, "modifying or locking"). Under the session setting with autocommit, `SELECT ... FOR SHARE` returned a row on 8.4.13. Layer 1 refuses locking clauses anyway.

### 2.2 Truncation without drain or discard

Capping a 729,000-row result at 100 rows (`bench.py big`):

| Engine | stream + discard (today) | stream + drain | **LIMIT max_rows+1 injected via sqlglot** |
|---|---|---|---|
| MySQL | 0.034 s, next call 9.6 ms (reconnect) | 1.361 s | **0.022 s, next call 0.5 ms** |
| MariaDB | 0.006 s, next call 10.5 ms | 1.409 s | **0.002 s, next call 0.4 ms** |
| PostgreSQL (named cursor) | 0.003 s | 0.003 s | 0.010 s (named) / **0.002 s (plain cursor)** |
| SQLite | ~0 | ~0 | 0.006 s |

Injecting `LIMIT max_rows+1` into the parsed statement makes the server stop producing rows. The connection stays reusable, and the reconnect cost from R#6 disappears. Over a 10 ms RTT link, that reconnect cost 104 vs 25 ms per call (`spikes/reconnect_cost.py`). The size cap (30,000 chars) still applies, but then at most `max_rows+1` rows are left to drain.

### 2.3 Per-call latency (200 sequential `SELECT 1`, localhost, pooled)

| Engine | raw driver | SQLAlchemy (defaults) | SQLAlchemy, `pool_reset_on_return=None` | + `pool_pre_ping=False` |
|---|---|---|---|---|
| MySQL | **0.38 ms** | 1.16–1.34 ms | 0.90 ms | 0.61 ms |
| MariaDB | 0.28 ms | 1.06 ms | | |
| PostgreSQL | 1.97 ms named cursor / **0.80 ms plain cursor** | 2.13–2.28 ms | 2.05 ms | 2.33 ms |
| SQLite | <0.01 ms | 0.02 ms | | |

The baseline is 0.67 ms (MySQL, `mysql_server.py` v2). Raw MySQL is faster than the baseline because it never sends the per-call `COM_INIT_DB` (R#18). Over a 10 ms RTT link, that extra `COM_INIT_DB` doubled `SELECT 1` latency: 25.45 vs 12.89 ms.

### 2.4 StarRocks: least privilege is not enough (spike `spikes/spike_starrocks.py`, `out-starrocks.txt`; docs S1–S8)

StarRocks speaks the MySQL protocol (PyMySQL works; `SELECT 1` 1.99 ms per call, which is the engine's planning cost). sqlglot has a `starrocks` dialect. But:

- **No engine read-only mode.** Layer 2 for writes does not exist, so layer 3 is the only real protection against writes.
- **A SELECT-only user can write files.** As `readonly_user`, with only `GRANT SELECT ON ALL TABLES IN DATABASE shop` (verified with `SHOW GRANTS`), both of these succeeded and wrote a 1,466-byte CSV of `shop.orders` into the container's `/tmp`:
  - `INSERT INTO FILES('path'='file:///tmp/...', 'format'='csv') SELECT * FROM shop.orders` (rowcount 40);
  - `SELECT * FROM shop.orders INTO OUTFILE 'file:///tmp/...' FORMAT AS CSV` (rowcount 40).

  The same statements can target S3/HDFS. Every other write test was refused by privileges (DELETE, INSERT, CREATE, SUBMIT TASK; error 5203). EXPORT needs its own privilege (doc S2).
- **The timeout is user-adjustable.** `SET query_timeout = 3600` is allowed for `readonly_user` (`SET GLOBAL` is not), and a `/*+ SET_VAR(query_timeout = 60) */` hint overrode a 1 s limit (13.09 s, completed).
- **Metadata.** No foreign keys: `key_column_usage` and `referential_constraints` are empty. `table_rows`/`data_length` read 0 right after loading.

Consequences, if StarRocks stays in release 1:
- The guard must refuse `Insert`, `Into`, `Set`, `Hint`, `Cache` and `Command` on StarRocks. The spike showed `INSERT INTO FILES` and `INTO OUTFILE` fail to parse, which fails closed.
- The server must also enforce its own client-side deadline: socket read timeout, then `KILL QUERY` from a side connection.
- The README must say loudly that **on StarRocks, the guard is the only barrier against file export, which violates the layer model**, and that operators should block it outside the server: for example, no `file://`/S3 credentials on the BE nodes, or a network policy.

**Owner decision (2026-10-28):** keep StarRocks in release 1, implemented **last**, with these warnings in the README.

## 3. Decision 1: library strategy

| Criterion | A: SQLAlchemy 2 Core | B: raw drivers + own `Dialect` classes |
|---|---|---|
| Lines of code (4 engines, spike) | **68** | 212 (includes a pool with max lifetime and the R#1 fix, plus per-engine introspection SQL) |
| Streaming | `stream_results` works (psycopg 3 is not named in its docs, doc #22). `result.close()` **drains** on MySQL: 1.42 s for a 729k-row result. Avoiding that needs `invalidate()` (a reconnect) or LIMIT injection. | full control; LIMIT injection plus a plain cursor |
| Read-only / timeout per connection | `connect` event (doc #23) runs the session SQL. The per-statement read-only *transaction* (the PostgreSQL bypass fix) fights SQLAlchemy's autobegin and reset-on-return. | explicit: `BEGIN READ ONLY` / `ROLLBACK` per statement, written once per dialect |
| Error mapping | wraps driver errors in `sqlalchemy.exc.*` with `.orig`. We still map codes per engine (1792, 3024, 1969, `QueryCanceled`, `interrupted`). | map the driver exceptions directly (the same per-engine table) |
| Latency vs 0.67 ms baseline | regresses with defaults (1.16 ms). Reaches 0.61 ms only with pre-ping off, which makes us write dead-connection handling ourselves anyway. | 0.38 ms |
| Dependency weight | +21 MB, import +119 ms (stdio servers start per session) | PyMySQL 0.4 MB (34 ms import), psycopg + binary 19 MB (84 ms) |
| Introspection | `inspect()` covers columns, FKs and indexes on every dialect, including SQL Server and Oracle. No incoming FKs (doc #24; `describe` had to scan every table). No table sizes, row estimates or comments, which `list_tables` returns today. | per-engine SQL (~25 lines per engine). Must be written for SQL Server and Oracle later. |
| Adding an engine | URL scheme + session SQL + error codes + any missing introspection | `Dialect` subclass (~50 lines: connect, session/transaction, introspection, error codes) |

**Recommendation: B, raw drivers behind a small `Dialect` interface.**

The safety rules this server needs live exactly where SQLAlchemy abstracts: transaction boundaries, the reset-on-return path, and cursor closing. Its defaults regress the latency baseline. Its strongest advantage, cross-dialect reflection, does not cover what `list_tables` and `describe_table` already return (sizes, estimates, comments, incoming FKs).

Revisit this if SQL Server or Oracle enter scope: their introspection is where SQLAlchemy saves the most code. Using `sqlalchemy.inspect` for *only* those engines is possible later, without adopting its pool or execution path.

## 4. Decision 2: the guard (layer 1) with sqlglot

**Proposal:** parse every statement with sqlglot for the profile's dialect, and accept it only if all of these hold:

1. The tokenizer and parser succeed. Catch `sqlglot.errors.SqlglotError`, not only `ParseError`: an unterminated literal raises `TokenError`, which would otherwise escape as an exception. Unparseable SQL is refused (fail closed).
2. Exactly one statement.
3. The root is `Select` or a set operation (`Union`, `Intersect`, `Except`). For MySQL and MariaDB parity with today, three more roots are allowed:
   - `Show`, unless its `WHERE` contains a subquery;
   - `Describe` of a table;
   - `Describe` of a SELECT with no `style` (this refuses both `EXPLAIN ANALYZE` and `DESC ANALYZE`).
4. No node anywhere of these types: `Insert`, `Update`, `Delete`, `Merge`, `Create`, `Drop`, `Alter`, `Command`, `Into`, `Lock`, `Set`, `Transaction`, `Commit`, `Rollback`, `Use`, `Copy`, `Pragma`, `Attach`, `Grant`, `LoadData`. Note that unsupported syntax becomes `Command` silently (doc #25), so it must be refused.
5. No `Hint` node on **any** dialect: hints override the server timeout on MySQL (`MAX_EXECUTION_TIME`) and StarRocks (`SET_VAR(query_timeout=...)`), both measured. MySQL-family dialects: no `/*!` executable comment either; sqlglot **drops** these from the AST, so check them at the token level. (The spike classifier lacked the hint rule and wrongly accepted `SELECT /*+ SET_VAR(query_timeout = 60) */ 1` for StarRocks.)
6. No function from the per-dialect deny list, matched by name, which also catches backtick-quoted names (`` `sleep`(3) ``):
   - MySQL/MariaDB: `sleep`, `benchmark`, lock functions, `load_file`, `*_pos_wait`, `wait_for_executed_gtid_set`;
   - PostgreSQL: `pg_sleep*`, `set_config`, advisory locks, `pg_read_*`, `lo_import`/`lo_export`, `dblink*`, `pg_terminate_backend`/`pg_cancel_backend`, `pg_reload_conf`;
   - SQLite: `load_extension`, `readfile`, `writefile`;
   - StarRocks: `sleep` (the spike accepted `SELECT SLEEP(3)` because no StarRocks list existed).
7. Pin the session settings the tokenizer assumes, so the guard and the server read the text the same way (R#8):
   - MySQL/MariaDB: remove `NO_BACKSLASH_ESCAPES` and `ANSI_QUOTES` from the session `sql_mode`;
   - PostgreSQL: `standard_conforming_strings=on`.

**Results (`spikes/out-sqlglot.txt`):**

- **81 cases, 0 mismatches** (49 MySQL, 1 MariaDB, 20 PostgreSQL, 7 SQLite, 2 T-SQL, 2 Oracle). The MySQL cases cover all 37 existing guard cases, all 6 regex false positives from R#11 (now accepted), and all 7 bypasses from R#8/R#10 (now refused).
  - PostgreSQL cases include data-modifying CTEs, `set_config`, `COPY`, `SELECT INTO` and `DO`.
  - SQLite cases include `ATTACH`, `PRAGMA` and `VACUUM INTO`.
  - **Caveat:** the spike classifier *refused* the four SHOW/DESCRIBE/EXPLAIN cases that `test_guard.py` allows, and that spike expectation was set to "refuse". Rule 3 (allowing them in restricted form) was checked only as AST shapes, not run: `Show` has a `where` arg in which subqueries can be detected, and `Describe` has `style='ANALYZE'` for both `EXPLAIN ANALYZE` and `DESC ANALYZE`. It must be implemented and run against the 37 cases in Phase 4.
- **False rejects on a legitimate-query corpus:**
  - MySQL: 30 of 32 accepted. Refused: `TABLE t` and `VALUES ROW(...)` (parsed as other roots).
  - PostgreSQL: 14 of 14 accepted (FILTER, DISTINCT ON, LATERAL, `::` casts, `WITHIN GROUP`, `TABLESAMPLE`, JSON operators).
  - `INTO OUTFILE`/`DUMPFILE` give a *parse error* and are refused, which is correct by accident: sqlglot does not support them.
- **Cost:** 0.38 ms per classification, 0.49 ms with column qualification. That is small next to a 0.4–0.8 ms round trip, but not free.
- **Does it remove R#11's false positives?** Yes, all six.
- **What it fails to parse:** see above. Also no MariaDB dialect exists (parse it as `mysql`; doc #25), and `EXPLAIN FOR CONNECTION n` is a parse error (refused).

**Masking by source column (R#4).** Qualify every column against the introspected schema (`sqlglot.optimizer.qualify`), resolve it to its real table, then apply one rule:

> A masked column may appear only as a bare column in the outermost SELECT list, where it is masked in the output. Any other use (an expression, WHERE, JOIN, a UNION branch, a derived table, a function argument) refuses the query.

Measured:
- `SELECT *`, `SELECT api_token`, and `SELECT api_token AS x` are allowed and masked.
- `CONCAT(...)`, UNION, derived tables, `EXTRACTVALUE` (error channel) and `WHERE api_token LIKE 'tok_a%'` (a boolean oracle not covered before) are all **refused**.
- Joins that do not touch masked columns are allowed.

This is still a safety net. It depends on the schema snapshot and the deny list, and column-level `GRANT`s remain the real control. The README must keep saying so.

**Refined in Phase 4 (P0), 2026-10-28.** An output column *derived* from a masked column (expression, UNION branch, CTE, derived table, scalar subquery) is returned as `***` instead of refusing the whole query. Reasons:
1. The unchanged smoke test reads the rows of `SELECT CONCAT(api_token,'') AS harmless_name`, so a refusal would break it.
2. A masked value gives the model nothing to misuse, while a refusal blocks the rest of the row.

The refusal still applies to masked columns in WHERE / JOIN / GROUP BY / HAVING / ORDER BY and to subqueries in predicates. When a query that reads a masked column fails, the engine's error text is replaced. Implemented in `src/sql_mcp/masking.py`, tested in `tests/unit/test_masking.py` and the live suite.

**Recommendation: adopt sqlglot (pinned to an exact version)** for the guard, LIMIT injection and masking. Keep the existing 37 offline cases as a regression suite, plus the 81-case and corpus suites.

## 5. Decision 3: configuration

- **Engine selection.** Use a `engine` key per profile (`mysql | mariadb | postgres | sqlite`) with discrete fields (`host`, `port`, `user`, `database`, `schema`, `path`), so secrets never sit inside a URL string. A profile without `engine` means `mysql` (compatibility). An optional `url` may be added later, but never with a password in it.
- **Secrets, in order of preference:**
  - `password_cmd`: an argv **list**, run without a shell, with a 10 s timeout. stdout is the secret, trimmed; stderr is logged without stdout. Example: `["security", "find-generic-password", "-s", "sql-mcp-prod", "-w"]`.
  - `password_env`: unchanged.
  - literal `password`: still accepted, with a warning on stderr and in `list_profiles`.
- **TLS** `tls: disable | prefer | require | verify-ca | verify-full`:

  | Mode | Encryption | Verification | Fallback |
  |---|---|---|---|
  | `disable` | none | none | n/a |
  | `prefer` | if offered | none | silent plaintext |
  | `require` | required | none | refuses plaintext |
  | `verify-ca` | required | chain checked against `tls_ca` | refuses plaintext |
  | `verify-full` | required | `verify-ca` + hostname | refuses plaintext |

  - PostgreSQL maps these 1:1 to libpq `sslmode`. PyMySQL maps through its `ssl` dict:
    - `require`: `CERT_NONE`, but `_ssl_required`;
    - `verify-ca`: `check_hostname=False`;
    - `verify-full`: `check_hostname=True`.
  - **Measured on MySQL 8.4.13:**
    - `verify-ca` with the server's CA connected (TLS 1.3, `CERT_REQUIRED`);
    - a wrong CA was refused;
    - `verify-full` against `127.0.0.1` was refused ("IP address mismatch"), because MySQL's auto-generated certificate has no SAN. `verify-full` therefore needs a properly issued certificate.
  - Any mode weaker than `verify-ca` logs a warning at first connect. `list_profiles` reports the *negotiated* state (`tls: "TLSv1.3, unverified"`), not just whether a CA was configured (R#7).
  - Default for profiles without `tls`: `prefer`, which is today's behaviour, plus a warning. New examples use `verify-full`.
- **Validation at load:**
  - `timeout_ms` must be 100 to 600,000; 0 is refused, because on MySQL 0 means no limit (R#9);
  - `pool_size` must be 1 to 20;
  - unknown keys are refused, with the key named.

## 6. Decision 4: layout and packaging

```
pyproject.toml                    # hatchling; ruff + mypy config; console script  sql-mcp = sql_mcp.server:main
src/sql_mcp/
  server.py      # MCPServer, tools/resources/prompts, ToolError translation (the only module that knows MCP)
  config.py      # profiles, engine selection, secrets (password_cmd), TLS modes, validation
  guard.py       # sqlglot classification, LIMIT injection, hint/executable-comment checks
  masking.py     # source-column masking policy
  results.py     # caps, cell trimming, compact {columns, rows}, structured output
  pool.py        # Condition-based pool: release/discard both wake waiters, max lifetime, idle ping
  audit.py       # optional JSON-lines audit log (P2)
  dialects/
    base.py      # Dialect interface: connect, run_read_only(sql, max_rows), introspection, error map
    mysql.py  mariadb.py  postgres.py  sqlite.py
mysql_server.py                   # compatibility shim: `python mysql_server.py` keeps working
tests/
  unit/          # guard (37 existing + 81 + corpus), masking, config, results, pool (no DB)
  conformance/   # one parametrized suite every engine must pass
  fixtures/      # shop + analytics seed per engine, one logical dataset
compose.yaml                      # Docker Compose profiles: mysql, mariadb, postgres (sqlite needs none)
```

Optional extras: `sql-mcp[mysql]` (PyMySQL), `sql-mcp[postgres]` (psycopg[binary]). SQLite is in the standard library. That makes `claude mcp add ... -- sql-mcp` possible after `pipx install`/`uv tool install`.

## 7. Decision 5: tests

- **Offline layer.** The guard suites above, masking policy cases, config validation, the pool's waiter wake-up (R#1 regression), and result caps.
- **Conformance suite.** One pytest module parametrized by engine, run against the same logical `shop`/`analytics` seed (MySQL databases map to PostgreSQL schemas and to SQLite files). Every engine must pass:
  - every tool's contract;
  - layer 2 with a *writer* account (writes refused) and with no guard (bypass attempts from §2.1 refused);
  - timeout at 1 s;
  - truncation with no reconnect;
  - stale-database/schema reset (R#2);
  - schema truncation reported (R#5);
  - masking cases.
- **Over stdio.** The existing `tests/smoke_mysql.py` (56 checks) stays unchanged. A per-engine smoke shares its client.
- **Performance.** `spikes/bench.py` becomes `bench/`, rerun per engine against the baselines in §2.
- **Release 1 engines (owner's choice):** MySQL 8.4, MariaDB 11.4 LTS, PostgreSQL 17, SQLite, StarRocks 4.1. The first four have an engine-enforced read-only mode and timeout that held in the spikes. StarRocks has neither (section 2.4). Docker image pinned for the tests: `starrocks/allin1-ubuntu:4.1.1` (amd64 + arm64 verified); it needs about 1 GB idle, capped at 3.5 GB.
- **Deferred:**
  - **SQL Server.** No session read-only mode exists. `ApplicationIntent=ReadOnly` falls back to the normal database, and pyodbc's `readonly` need not be enforced (doc #18). Least privilege would be the **only** protection, and the README must say so loudly.
  - **Oracle.** `SET TRANSACTION READ ONLY` per transaction plus `call_timeout` per round trip (doc #20–21).
  - Neither was spiked. Their Docker images were not checked.

## 8. Decision 6: compatibility and migration

A MySQL-only user changes nothing:
- `MYSQL_CONFIG` and the `MYSQL_*` single-profile variables are still read;
- a profile without `engine` is MySQL;
- `python mysql_server.py` still launches the same tools.

**Naming.** For MySQL and MariaDB, "database" and "schema" are the same thing. For PostgreSQL the connection database is fixed per profile, and the tools' `database` argument means **schema**. Tools gain a `schema` parameter, with `database` kept as an alias. `list_databases` keeps its name and lists schemas on PostgreSQL, saying so in its description.

**Conflicts with "the existing checks pass unchanged".** The owner must decide these:

1. `smoke_mysql.py` asserts `serverInfo == {"name": "mysql-server", "version": "2.0.0"}`. Options:
   - the shim keeps reporting exactly that;
   - the check is updated (a change to the test).
2. `test_guard.py` asserts `check_ident("a b")` raises, while P0 wants legitimate quoted names to work. Proposal:
   - keep `check_ident` unchanged for the test;
   - tools resolve names by exact lookup in the catalog and quote them through the dialect;
   - a name that fails both the lookup and the old regex still gets "Invalid table name", so the smoke injection checks still pass.
3. The `.env`-driven port and the `tempfile` copy in `smoke_mysql.py` (Phase 1) write outside the project only when the port differs from the JSON.

## 9. Owner decisions (2026-10-27)

1. Engines in release 1: MySQL, MariaDB, PostgreSQL, SQLite, **and StarRocks**. Order: MySQL parity, MariaDB, PostgreSQL, SQLite, then StarRocks last (section 2.4).
2. Library: **raw drivers behind our own `Dialect` classes**.
3. Guard and masking: **sqlglot, pinned** to an exact version.
4. Name and license: package `sql-mcp`, import `sql_mcp`, command `sql-mcp`, **MIT**.
5. `serverInfo`: the `mysql_server.py` shim keeps reporting `mysql-server` / `2.0.0`; `sql-mcp` reports its own name and version.
6. Identifiers: tools resolve names by exact catalog lookup and quote them per dialect; `check_ident` stays unchanged.
7. The smoke test writes its patched profile to the git-ignored `.smoke/` folder. This is done: 56/56 pass, and the two temp dirs are deleted.

## 10. Consequences and risks

- sqlglot becomes security-relevant code from a third party. Pin an exact version, and run the guard suites on every upgrade. Its MySQL support has gaps (no MariaDB dialect; `INTO OUTFILE` and `TABLE` not parsed), which fail closed.
- Masking by lineage can refuse legitimate analytics that touch secret columns (for example `COUNT(DISTINCT email)`). That is intended. The refusal message must say how to ask for a view or a GRANT.
- Per-statement transactions add one round trip on PostgreSQL. With a plain cursor, PostgreSQL measured 0.80 ms per call.
- Not verified:
  - SQL Server and Oracle behaviour;
  - TLS `verify-full` with a properly issued certificate;
  - CI behaviour on Linux runners (the smoke test passed 56/56 under 8 CPU-burning processes on macOS);
  - whether Claude Code passes both `content` and `structuredContent` to the model (R#13).
