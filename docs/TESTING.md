# How to test sql-mcp

Everything below runs from the project root. Docker is at `~/.docker/bin/docker`:
```bash
export PATH="$HOME/.docker/bin:$PATH"
```
All test credentials are TEST-ONLY. The containers listen on `127.0.0.1` only and never touch your
`claude-mcp-mysql`, Trino, or Spark containers.

## 0. One-time setup
```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[postgres,dev]'     # mcp, PyMySQL, sqlglot, psycopg, ruff, mypy
```

## 1. Offline tests (fast, no Docker) — guard, masking, config, pool, results, audit
```bash
.venv/bin/python -m unittest discover -s tests          # expect: Ran 54 tests ... OK
.venv/bin/ruff check src tests bench mysql_server.py    # expect: All checks passed!
.venv/bin/mypy                                           # expect: Success: no issues found
```

## 2. MySQL: the original contract is unchanged
```bash
docker compose up -d --wait                              # sql-mcp-mysql on 127.0.0.1:33316
.venv/bin/python -m unittest discover -s tests -p 'test_guard*.py'   # the 37 legacy guard cases, still passing
.venv/bin/python tests/smoke_mysql.py                   # expect: ALL CHECKS PASSED (56 PASS)
.venv/bin/python -m unittest discover -s tests -p 'live_*.py'        # one regression test per REVIEW.md finding
.venv/bin/python bench/perf_mysql.py                    # performance; compare to bench/results/
```

## 3. The conformance suite — every engine passes the same checks
One suite, selected by `SQL_MCP_ENGINE`. It covers the tool contract (partly over real stdio by launching
`sql-mcp`), engine-enforced read-only with a writer account, the timeout, pooling/kill/retry, masking,
awkward identifiers, TLS, and every review finding.

```bash
# MySQL (already up from step 2)
SQL_MCP_ENGINE=mysql    .venv/bin/python -m unittest tests.conformance.conformance_suite -v

# MariaDB 11.4 (default); add 10.11 and 11.8 with the matrix profile
docker compose -f docker/mariadb.compose.yml --profile matrix up -d --wait
SQL_MCP_ENGINE=mariadb  .venv/bin/python -m unittest tests.conformance.conformance_suite -v                # 33326 = 11.4
SQL_MCP_ENGINE=mariadb SQL_MCP_CONFORMANCE_PORT=33327 .venv/bin/python -m unittest tests.conformance.conformance_suite  # 10.11
SQL_MCP_ENGINE=mariadb SQL_MCP_CONFORMANCE_PORT=33328 .venv/bin/python -m unittest tests.conformance.conformance_suite  # 11.8

# PostgreSQL 17 (default); 14 and 18 with the matrix profile. make-certs.sh is a one-time test CA for TLS.
docker/pg-tls/make-certs.sh
docker compose -f docker/postgres.compose.yml --profile matrix up -d --wait
SQL_MCP_ENGINE=postgres .venv/bin/python -m unittest tests.conformance.conformance_suite -v                # 54329 = 17
SQL_MCP_ENGINE=postgres SQL_MCP_CONFORMANCE_PORT=54314 .venv/bin/python -m unittest tests.conformance.conformance_suite  # 14
SQL_MCP_ENGINE=postgres SQL_MCP_CONFORMANCE_PORT=54318 .venv/bin/python -m unittest tests.conformance.conformance_suite  # 18

# SQLite (no Docker; builds read-only files under .smoke/sqlite)
SQL_MCP_ENGINE=sqlite   .venv/bin/python -m unittest tests.conformance.conformance_suite -v

# StarRocks 4.1.1 (seeds itself on first run; SQL_MCP_STARROCKS_TAG=3.5.12 for the 3.5 line)
docker compose -f docker/starrocks.compose.yml up -d --wait
SQL_MCP_ENGINE=starrocks .venv/bin/python -m unittest tests.conformance.conformance_suite -v
```
Each prints `OK (skipped=N)`. Skips are expected and self-describing (e.g. SQLite has no network, so TLS is
skipped; StarRocks has no read-only mode, so that check is replaced by the "documented gap" tests).

## 4. StarRocks TLS (MySQL protocol over TLS)
```bash
docker compose -f docker/starrocks.compose.yml up -d --wait
./docker/starrocks-tls.sh        # makes a JKS cert for 127.0.0.1, enables TLS, restarts; writes docker/sr-tls/ca.pem
SQL_MCP_ENGINE=starrocks .venv/bin/python -m unittest tests.conformance.conformance_suite.Tls -v
# verify-full succeeds with the right CA and fails with a wrong one; disable is plaintext.
```

## 5. StarRocks external catalog (Iceberg) through sql-mcp
This proves you can read an Iceberg lake read-only through the server, and that masking still works on
external tables (whose columns are not in information_schema).
```bash
docker compose -f docker/starrocks.compose.yml --profile ext up -d --wait      # + SeaweedFS (S3) + Iceberg REST
docker exec sql-mcp-seaweedfs sh -c "echo 's3.bucket.create -name warehouse' | weed shell"   # one-time, per fresh container
.venv/bin/python tests/fixtures/make_external_catalogs.py iceberg
SQL_MCP_SR_CATALOG=iceberg_cat .venv/bin/python -m unittest tests.conformance.starrocks_external -v   # 5 tests
```
Key check inside it: `SELECT api_token AS x FROM events` comes back as `***` even though `events` is an
Iceberg table and the column is aliased.

**Hive** (`hive_cat`) uses the identical code path. The metastore is wired up (`--profile ext` starts it, and
`docker/hive-lib/bundle-2.24.6.jar` must be present — see `docker/hive-lib/`), but live Hive *data* creation is
blocked by a SeaweedFS S3A `mkdirs` quirk, so `make_external_catalogs.py hive` skips and the Hive conformance
run skips with a clear message. Nothing in sql-mcp is Hive-specific.

## 6. A real Claude Code session (nothing registered, ~/.claude.json untouched)
Loads the server only for one run from a project-local config.
```bash
# .smoke/mcp-config.json and .smoke/claude-connections.json were written during the build; recreate if needed.
env -u ANTHROPIC_API_KEY claude -p \
  "Using only the sql tools, how many rows are in the orders table of the shop database? Reply with just the number." \
  --mcp-config .smoke/mcp-config.json --strict-mcp-config \
  --allowedTools mcp__sql__list_tables mcp__sql__execute_query mcp__sql__describe_table < /dev/null
# -> 40.   The audit log at .smoke/claude-session-audit.jsonl shows the exact SELECT it ran.
```
`env -u ANTHROPIC_API_KEY` makes it use your claude.ai login (the `ANTHROPIC_API_KEY` in this shell has no
credit). A masking question ("show me the api_token of the first app_user") returns `***` to the model.

## 7. Shared-data StarRocks (optional, not stood up here)
sql-mcp talks to the StarRocks FE over the MySQL protocol, which is identical whether the cluster is the
all-in-one image (tested above) or a shared-data `fe` + `cn` + object-store deployment — the server has no
knowledge of the storage mode. To exercise it anyway, use StarRocks' official quickstart compose
(`github.com/StarRocks/demo .../quickstart/docker-compose.yml`, `run_mode=shared_data`), create a storage
volume, then point a `{"engine":"starrocks","port":<fe port>}` profile at it and run the conformance suite.
It was left out of the live runs only to spare memory next to your other containers.

## Tearing down
```bash
docker compose down -v                                           # MySQL
docker compose -f docker/mariadb.compose.yml --profile matrix down -v
docker compose -f docker/postgres.compose.yml --profile matrix down -v
docker compose -f docker/starrocks.compose.yml --profile ext down -v
rm -rf .smoke                                                    # generated test configs, SQLite files, audit logs
```
`docker/hive-lib/bundle-2.24.6.jar` (532 MB, git-ignored) can be deleted if you are done with Hive.
