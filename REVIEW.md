# Review of `mysql_server.py` v2 (Phase 2)

Scope: all of `mysql_server.py` (638 lines), `tests/test_guard.py`, `tests/smoke_mysql.py`, README, and the
library code the server relies on (PyMySQL 1.2.3, mcp 2.3.0). No behaviour was changed.

Environment for every reproduction below: Python 3.15.0, the `sql-mcp-mysql` test stack (MySQL 8.4.13,
`127.0.0.1:33316`), the seeded `shop`/`analytics` data, and the test accounts. Live reproductions import the
module the same way `tests/smoke_mysql.py` Part B does, with a scratch profile file that adds two profiles:
`nodb` (no default database, `pool_size: 1`) and `one` (`database: shop`, `pool_size: 1`).

Status tags:
- **CONFIRMED**: reproduced in this session, or proved directly from the code or library source.
- **SUSPECTED**: plausible, not reproduced.
- **REFUTED**: the claim does not hold as stated.

Severity reflects impact in the intended use: a model querying a real (possibly production) database through
a least-privilege account.

## Summary table

| # | Sev | Area | Finding | Status |
|---|---|---|---|---|
| 1 | High | concurrency | Waiters starve for 10 s and fail "busy" when a busy connection is discarded instead of released | CONFIRMED |
| 2 | High | correctness | With no default database, a pooled connection keeps the database a previous call selected | CONFIRMED |
| 3 | High | correctness, safety | `SHOW ... WHERE (subquery)` escapes the server timeout; the 15 s read timeout plus retry leaves 2 orphan queries running | CONFIRMED |
| 4 | High | security | Masking is bypassed by UNION, derived tables and error messages, not only by expressions | CONFIRMED |
| 5 | Medium | correctness | Every internal schema read is silently truncated at 30,000 chars; `schema://` showed 59 of 78 tables | CONFIRMED (prior finding 5 REFUTED as stated) |
| 6 | Medium | performance | Every truncated or failed query discards its connection: 9.33 vs 0.66 ms per call, 99 reconnects per 100 calls | CONFIRMED |
| 7 | Medium | security | TLS with no `ssl_ca` is encrypted but unverified, silently falls back to plaintext, and `list_profiles` says `tls: false` | CONFIRMED |
| 8 | Medium | security | Guard and server tokenize differently under `NO_BACKSLASH_ESCAPES`; an `INTO OUTFILE` passes the guard | CONFIRMED |
| 9 | Medium | config | `timeout_ms: 0` silently disables the timeout; `pool_size <= 0` makes every call wait 10 s and fail | CONFIRMED (code + MySQL docs) |
| 10 | Low | security | Guard gaps: `` `sleep`(3) ``, `DESC ANALYZE`, `FOR SHARE`, `LOCK IN SHARE MODE`, `SOURCE_POS_WAIT` are allowed | CONFIRMED |
| 11 | Low | correctness | Guard false positives: `load`, `call`, `handler` as names, `1--1`, `(SELECT 1) UNION (SELECT 2)` | CONFIRMED |
| 12 | Low | API | Identifier regex blocks hyphen, space and non-ASCII names, including the `database` argument | CONFIRMED |
| 13 | Low | API | Results are sent twice on the wire (text + `structuredContent.result` as an escaped string); the output schema is trivial | CONFIRMED |
| 14 | Low | ops | Pool design: no max lifetime, `conn._last_used` attribute hack, global `_pools`, `lru_cache` config; pools outlive config | CONFIRMED |
| 15 | Low | security, ops | Literal `password` accepted; no `password_cmd` | CONFIRMED (code) |
| 16 | Low | ops | Stderr-only logs, no query IDs or counters; each discard prints an ignored `AttributeError` traceback | CONFIRMED |
| 17 | Low | correctness | A bad `ssl_ca` path raises `FileNotFoundError`, which the SDK masks as a bare error | CONFIRMED |
| 18 | Low | performance | `conn.db` is bytes and the target is str, so `COM_INIT_DB` is sent on every call (20 of 20) | CONFIRMED |
| 19 | Low | correctness | Over-masking by substring (`ssn` matches `classname`; `password` matches `password_changed_at`) | CONFIRMED |
| 20 | Low | portability | Introspection, `EXPLAIN` and `max_execution_time` are MySQL-specific; MariaDB likely fails at connect | CONFIRMED (code) / SUSPECTED (MariaDB) |
| 21 | Low | tests | Weak over-the-wire guard checks (3 of 6 pass with the guard off), no TLS, pool-discard or stale-db tests, timing thresholds, no packaging or CI | CONFIRMED |
| 22 | Low | docs | README says TLS "(tested)", but no test exists; layer numbering in README is the reverse of the docstring | CONFIRMED |
| 23 | Info | security | Query results are untrusted text that goes to the model (prompt injection via data) | design note |

Each item in the brief's list of 13 known issues maps to a finding here (the brief's number is first):
1→11 and 8 · 2→4 · 3→7 · 4→12 · 5→5 · 6→6 · 7→14 · 8→15 · 9→16 · 10→13 · 11→3 and 20 · 12→21 · 13→20.

---

## High

### 1. Pool waiters starve when a connection is discarded (concurrency) — CONFIRMED

`Pool.acquire` (`mysql_server.py:180-206`) only waits on the idle queue (`self._idle.get(timeout=10)`, line 196).
`Pool.discard` (lines 212-218) decrements `_created` but never wakes a waiter.

`_run_once` discards rather than releases on two paths:
- every truncated result (lines 336-337);
- every error (`healthy` stays `False`, lines 359-362).

So a waiter blocked at line 196 never sees the capacity that was freed. It times out after 10 s with "All N connections ... are busy", even though `_created < pool_size`.

Reproduction (profile `one`, `pool_size: 1`):
- Thread A runs `SELECT o.id FROM (SELECT SLEEP(1) AS s) x, shop.orders o` with `max_rows=1`, so it is truncated and discarded.
- Thread B starts 0.2 s later with `SELECT 1`.

```
A (truncated -> discarded): truncated='row limit 1'
B (waiting for a connection): ToolError after 10.0s: All 1 connections of profile 'one' are busy.
```

Correct behaviour would be for B to finish after about 1 s. This matters because truncation is the *normal* outcome of model-written queries without a LIMIT. Under any concurrency, users get spurious 10 s stalls and "busy" errors.

The smoke test's concurrency check uses `SELECT SLEEP(0.3)`, which is never truncated and never fails, so it cannot see this.

**Fix:** guard capacity with a `threading.Condition` or a `BoundedSemaphore`, so that both release and discard wake waiters.

### 2. Stale database on pooled connections when the profile has no default (correctness) — CONFIRMED

`_run_once` calls `select_db` only when a target exists (`target = database or p.database; if target and ...`, lines 302-304). A profile without `database` (single-profile env mode without `MYSQL_DATABASE`, or any profile that omits it) therefore runs an unqualified query against **whatever database the previous call on that connection selected**.

Profile `nodb`, `pool_size: 1`:

```
1st call, database='analytics': rows [['analytics', 3]]
2nd call, NO database        : rows [['analytics', 3]]          <- should be "No database selected"
same 2nd call on a fresh pool: ToolError: MySQL error: (1046, 'No database selected')
```

With `pool_size > 1` the answer also depends on which connection you get, so it is nondeterministic. On a server that holds `app_prod` and `app_staging` with the same table names, this silently returns data from the wrong one.

**Fix:** always reset the database, or require a database for unqualified queries. Note the related finding 18: the `conn.db != target` comparison can never be false.

### 3. `SHOW ... WHERE` escapes the timeout; read timeout plus retry leaves orphan queries (correctness, safety) — CONFIRMED

Three pieces of code combine here:
- Layer 2 is `max_execution_time` (line 175), which MySQL applies to SELECT statements only.
- The guard lets `SHOW` through without the keyword scan (lines 419-422).
- `SHOW` accepts a `WHERE` clause with arbitrary subqueries.

A plain SELECT is killed at the limit; the same work inside `SHOW` is not. As root, with `SET SESSION max_execution_time=1000`, using a 4-way cross join with a `WHERE`:

```
plain SELECT              1.3s  ERROR 3024 ... maximum statement execution time exceeded
SHOW ... WHERE subquery   2.5s  analytics information_schema mysql performance_schema shop sys   (ran to completion)
EXPLAIN ANALYZE           1.2s  partial plan, Aggregate "actual time=1001 rows=0" (IS bounded)
```

Through `execute_query` (profile timeout 5000 ms), with a 5-way cross join inside `SHOW DATABASES WHERE (...) >= 0`:

```
after 30.0s: ToolError: MySQL connection was lost twice in a row. Is the server up?
still running on the server: 1378  30s  SHOW DATABASES WHERE (SELECT COUNT(*) FR
                             1382  15s  SHOW DATABASES WHERE (SELECT COUNT(*) FR
```

(I killed both afterwards.)

What happens:
1. PyMySQL's `read_timeout` (15 s, line 173) fires and is reported as error 2013.
2. `run()` (lines 270-282) treats that as a dead server and **re-runs the query**.
3. The server keeps executing the first copy, because MySQL does not notice a client that went away while it computes.

The result: one tool call left two runaway queries on the server, plus a misleading error message.

**Fix:**
- Never retry after a read timeout; retry only when the connection failed *before* the statement was sent.
- Kill the server-side query on client timeout (`KILL QUERY <thread_id>` from a side connection).
- Either apply the guard's keyword scan to `SHOW`, or refuse subqueries inside `SHOW`.

### 4. Masking is bypassed by UNION, derived tables and error messages (security) — CONFIRMED

Prior finding 2 is confirmed and broader than known. Masking compares the output label and PyMySQL's
`fields[i].org_name` (lines 309-315); `org_name` is a PyMySQL internal, `cur._result.fields`. The analytics profile masks `password`, `token` and `email`.

```
SELECT api_token AS x FROM app_users                         -> [['***'], ['***']]          masked
SELECT 'n/a' AS x UNION ALL SELECT api_token FROM app_users  -> ['n/a'], ['tok_ann_0123...'] LEAKED (metadata comes from the 1st branch)
SELECT t.x FROM (SELECT api_token AS x FROM app_users) t     -> ['tok_ann_...'], ['tok_bob_...'] LEAKED (org_name is 'x')
SELECT EXTRACTVALUE(1, CONCAT(0x7e,(SELECT api_token ...)))  -> ToolError: XPATH syntax error: '~tok_ann_0123456789abcdef012345'  LEAKED via error
```

The error path (`_friendly`, line 260) passes server messages through verbatim and is never masked. Name-based masking cannot be fixed by more heuristics.

**Real protection:** column-level `GRANT`s, or views that exclude the column. A parser with lineage (Phase 3) could make the safety net catch UNION and derived tables, but not error-based or arbitrary-expression leaks. The README must keep saying "safety net".

## Medium

### 5. Internal schema reads are silently truncated; `schema://` shows a partial schema (correctness) — CONFIRMED

Prior finding 5 is REFUTED as stated. The resource *is* capped, because `rows_of` → `run` → `_run_once` applies `MAX_CHARS = 30,000` (lines 50, 329-331). But `rows_of` (lines 365-367) throws the `truncated` flag away, so the cut is **silent**.

The same applies to `describe_table`, `search_schema`, `list_databases` and the resource. The partial result is also cached for 60 s (`cached`, lines 375-387).

```
resource lists 59 tables (16810 chars); information_schema has 78;
underlying query: rows=571 truncated='size limit 30000 chars' (rows_of() drops this flag)
```

The model is told it has "every table and column in a database" (docstring, line 593) when it has 76% of the tables. On a real schema with hundreds of tables, this is the common case.

**Fix:** `rows_of` must raise or report truncation. The resource needs its own explicit cap with a "truncated: N more tables" marker.

### 6. Discarding connections on truncation and on error costs 14x per call (performance) — CONFIRMED

Prior finding 6 has now been measured. Each test is 100 calls through `ms.run` on the `shop` profile:

```
LIMIT 10, not truncated : 0.66 ms/call, new connections: 0
max_rows=10, truncated  : 9.33 ms/call, new connections: 99
10 failing queries (unknown column) opened 9 new connections
```

The 0.66 ms figure matches the README baseline. On localhost, the reconnect (TCP + TLS 1.3 + auth + `init_command`) costs about 8.7 ms. Against a remote database it will cost a network round trip several times over (SUSPECTED; not measured).

Discarding after a *failed* statement is unnecessary: the server has already sent its error packet and the connection is reusable.

**Options for P2:**
- For truncation, keep the discard only when unread rows exceed a threshold; otherwise drain.
- Add a server-side `LIMIT max_rows+1` wrapper where the dialect allows it.
- On errors, keep the connection unless the error was a lost connection.

### 7. TLS: unverified by default, silent plaintext fallback, misreported (security) — CONFIRMED

Prior finding 3 is confirmed and explained from source. With no `ssl` argument, PyMySQL 1.2.3 uses a "PREFERRED" mode:
- `pymysql/connections.py:300-305` creates `_create_ssl_ctx({})`, which yields `CERT_NONE` and `check_hostname=False`;
- lines 941-942 fall back to plaintext if the server does not advertise SSL.

```
session: Ssl_cipher=TLS_AES_256_GCM_SHA384, Ssl_version=TLSv1.3
client ctx.verify_mode=CERT_NONE check_hostname=False _ssl_required=False
```

So by default the link is encrypted but anyone in the middle can impersonate the server, and a stripped capability flag downgrades it to plaintext without a word. Meanwhile `list_profiles` reports `"tls": bool(p.ssl_ca)` (line 455), i.e. `false`, for a session that is in fact TLS.

With `ssl_ca` set, PyMySQL treats TLS as required and raises on downgrade (lines 293-298, 935-939). That is correct, but it is SUSPECTED for this server: no CA-based test exists (see finding 22).

The server has no way to express `disable`, `require` or `verify-full`, and nothing warns when verification is off.

### 8. Guard and server disagree under `NO_BACKSLASH_ESCAPES` (security) — CONFIRMED

`_LITERALS` (line 396) always treats `\'` as an escape. When the server's `sql_mode` includes `NO_BACKSLASH_ESCAPES`, MySQL does not.

Guard (offline):
```
ALLOWED | SELECT 'a\', 1 INTO OUTFILE '/tmp/x'
```

Server (as root): the same text tokenizes differently once the mode is on:
```
SELECT 'a\', 1 AS second_col                                         -> ERROR 1064 (unterminated literal)
SET SESSION sql_mode='NO_BACKSLASH_ESCAPES'; SELECT 'a\', 1 AS ...   -> a\   1      (two columns)
```

The guard's scrubbed text hides `INTO OUTFILE` inside what it thinks is a literal. The server, in that mode, would execute `INTO OUTFILE`.

Mitigations that hold today:
- `INTO OUTFILE` needs the `FILE` privilege (layer 3; `readonly_user` gets `1227 Access denied`);
- `secure_file_priv`.

The session never sets `sql_mode`, so the guard's parse depends on the server's global setting. The README already mentions this.

**Fix:** pin the relevant `sql_mode` flags in `init_command`, or parse with a real tokenizer for the session's mode.

### 9. Unvalidated `timeout_ms` and `pool_size` (config) — CONFIRMED (code + MySQL docs)

`timeout_ms=int(raw.get("timeout_ms", 5000))` and `pool_size=int(...)` (lines 108-109) accept any integer:
- `timeout_ms: 0` becomes `max_execution_time=0` (line 175). The MySQL 8.4 reference manual, under the `max_execution_time` system variable, says 0 means timeouts are not enabled. One typo silently removes layer 2's timeout.
- A negative value makes the `SET` fail on every connect.
- `pool_size <= 0` makes `_created < pool_size` always false (line 185), so every call waits 10 s and fails "busy".

I did not run these. The effect follows directly from the code, and the meaning of 0 is from the MySQL documentation.

## Low

### 10. Guard gaps: things it allows that it means to refuse (security) — CONFIRMED

```
ALLOWED | SELECT `sleep`(3)                  <- server runs it: SELECT `sleep`(0.1) returned 0
ALLOWED | DESC ANALYZE SELECT 1               <- server executes it (EXPLAIN ANALYZE is blocked, its synonym is not)
ALLOWED | SELECT id FROM t FOR SHARE          <- server allows it in a read-only session (returned 1)
ALLOWED | SELECT id FROM t LOCK IN SHARE MODE
ALLOWED | SELECT SOURCE_POS_WAIT('f', 4, 10)  <- 8.4 name; only MASTER_POS_WAIT is listed (line 400)
ALLOWED | SHOW DATABASES WHERE (SELECT COUNT(*) FROM a, b) > 0   <- see finding 3
```

Why each one gets through:
- **Backtick-quoted function names:** literal blanking (line 407) also blanks quoted identifiers, so the function name becomes invisible to `_RISKY_FUNCS`.
- **`DESC ANALYZE`:** the `DESC`/`DESCRIBE` verbs skip the keyword scan, and only `explain\s+analyze` is matched (line 417).

Impact is bounded:
- `max_execution_time` stops the SELECT-based cases: `DESC ANALYZE` stopped at 1001 ms in the finding 3 test.
- Locking reads in autocommit hold shared locks only for the statement. On a busy primary, they can still block writers for up to the timeout.

### 11. Guard false positives (correctness) — CONFIRMED

Prior finding 1 is confirmed:
```
BLOCKED (write or file keyword) | SELECT load FROM servers
BLOCKED (write or file keyword) | SELECT c.amount AS call FROM t c
BLOCKED (write or file keyword) | SELECT h.* FROM handler h
BLOCKED (SQL comments)          | SELECT 1--1          (MySQL needs whitespace after -- for a comment)
BLOCKED (verb '(select')        | (SELECT 1) UNION (SELECT 2)
```
The guard is a regex over text with literals blanked (lines 396-423), not a parser. Its correctness depends on MySQL's quoting and comment rules, which do not carry over to other engines.

### 12. Identifier regex excludes legitimate names (API) — CONFIRMED

Prior finding 4 is confirmed. `IDENT = ^[A-Za-z0-9_$]{1,64}$` (line 53):
```
'order-archive': Invalid table name ...   'Order History': Invalid ...   'café': Invalid ...
```

The same regex guards the `database` argument (`resolve_db`, line 430). So a database named `my-app` can be reached only as a profile's default (which is never validated), never through `database=`.

`execute_query` can still reach such tables with backticks, because the guard blanks them. Only `describe_table` and `sample_rows`, and anything that passes `database=`, are affected.

### 13. Output shape: double payload, trivial schema (API) — CONFIRMED

Prior finding 10 is confirmed. Over stdio:
```
outputSchema(execute_query): {"properties":{"result":{"type":"string"}},"required":["result"], ...}
structuredContent: {"result": "{\"columns\":[\"a\"],\"rows\":[[1]],\"row_count\":1,\"ms\":1}"}
```

Because the tools are annotated `-> str`, the SDK wraps the JSON *string* as `structuredContent.result`. Every result is therefore sent twice: once as text, once escaped (even larger).

The README's 1,049-char figure counts one copy. Whether a given client feeds both copies to the model is SUSPECTED, not checked.

Errors carry no `structuredContent`, which is fine.

### 14. Pool and config lifecycle (ops) — CONFIRMED

Prior finding 7 is confirmed:
- no maximum connection age or recycling;
- `conn._last_used` is an attribute set on a PyMySQL object (lines 177, 200, 209);
- module-global `_pools` keyed by profile **name** (lines 231-239);
- `config()` is `lru_cache`d (line 116), so config edits need a restart.

An extra consequence: clearing the config cache would not rebuild pools. `pool_for` returns the old `Pool` holding the old `Profile`; the smoke test has to clear both by hand (`smoke_mysql.py`, the wrong-password block).

### 15. Secrets (security, ops) — CONFIRMED (code)

Prior finding 8 is confirmed:
- `_profile` accepts a literal `password` (line 95);
- single-profile mode reads `MYSQL_PASSWORD` (line 141);
- there is no `password_cmd`.

That `claude mcp add --env` stores the secret in plain text in `~/.claude.json` comes from the README's own text (line 47). I did not re-check it, because that file is off limits for this work.

### 16. Logging (ops) — CONFIRMED

Prior finding 9 is confirmed: stderr only (lines 61-62, 346-349), with no audit file, request or query IDs, or per-profile counters.

New: every discarded connection with an unread `SSCursor` makes PyMySQL's finaliser try to drain a closed socket. The result is an `Exception ignored ... AttributeError: 'NoneType' object has no attribute 'settimeout'` traceback on stderr (seen many times in the reproduction output). It is harmless, because it goes to stderr and not to the protocol channel, but it buries real log lines.

### 17. Non-PyMySQL errors are masked from the model (correctness) — CONFIRMED

With a profile whose `ssl_ca` points to a missing file:
```
NOT a ToolError -> SDK masks it as a bare 'Error executing tool': FileNotFoundError [Errno 2] ...
```
`_run_once` only converts `pymysql.MySQLError` (lines 293-295, 351-357), so configuration mistakes like this reach the model as an unexplained failure.

### 18. `COM_INIT_DB` on every call (performance) — CONFIRMED

PyMySQL re-encodes `conn.db` to bytes during the handshake (`pymysql/connections.py:994`), and `select_db` never updates it (line 521). So `conn.db != target` (line 303) compares `b'shop'` with `'shop'` and is always true:

```
conn.db = b'shop', target = 'shop', conn.db != target -> True
COM_INIT_DB count grew by 20 over 20 calls to the same database
```

That is one extra round trip per call. It is cheap on localhost (already included in the 0.66 ms), but it is still a round trip on a remote server.

### 19. Over-masking (correctness) — CONFIRMED

Substring matching (line 315) masks innocent columns: `'ssn' in 'classname'` is `True`, and `password` matches `password_changed_at`. This is a usability cost only.

### 20. MySQL-specific code; MariaDB untested (portability) — CONFIRMED (code) / SUSPECTED (MariaDB)

Prior findings 11 and 13:
- The introspection queries use MySQL columns: `column_type`, `column_key`, `extra`, `engine`, `data_length`, `table_rows` (lines 475-494, 598-603).
- `explain_query` returns MySQL's tabular EXPLAIN.
- `max_execution_time` covers SELECT only (finding 3).

MariaDB was not run in this phase. From memory, to verify in Phase 3: MariaDB has no `max_execution_time` (it uses `max_statement_time`, in seconds). If so, the `init_command` (line 175) fails and **every connection** to MariaDB fails. That is SUSPECTED, not verified.

### 21. Tests (tests) — CONFIRMED

- **Weak over-the-wire guard checks.** `smoke_mysql.py` checks `err and "Error executing tool" in text and len(text) > 40`. That holds for *any* error, including errors from layers 2 and 3. With `check_read_only` replaced by a no-op:
  ```
  DROP     smoke check would STILL PASS   Rejected: this connection is read-only.
  stacked  smoke check would STILL PASS   MySQL error: (1064, ...)
  outfile  smoke check would STILL PASS   MySQL error: (1227, 'Access denied; you need ... FILE ...')
  ```
  3 of the 6 checks do not test the guard at all.
- **Missing tests:** none for TLS, the pool discard paths (finding 1), the stale database (finding 2), schema truncation (finding 5), masking through UNION or derived tables (finding 4), or `SHOW` timeouts (finding 3). This is why these bugs survived 93 passing checks.
- **Timing thresholds:** `< 2s`, `4 < t < 15`, `1.0 < t < 3.0` make the smoke test sensitive to load on shared CI runners (SUSPECTED flakiness).
- Prior finding 12 is partly REFUTED: nothing in `smoke_mysql.py` is macOS-specific (only the `~/.docker/bin/docker` fallback path). It has only ever been *run* on macOS. It does need Docker and `docker exec`.
- There is no `pyproject.toml`, CI, linter or type checker (`ls` confirms). Tests load the module by file path.

### 22. Docs (docs) — CONFIRMED

- README line 78 says TLS verification is "(tested)". No test in the repo exercises `ssl_ca`; a grep of `tests/` finds nothing.
- The README numbers the layers 1 = user, 2 = session, 3 = guard (lines 56-58). The module docstring and the smoke test use the reverse (3 = user, 1 = guard; lines 19-21). README line 103 ("Layers 1-2 above") only reads correctly with the README's own numbering. Pick one scheme.

### 23. Data is untrusted input to the model (security) — design note

Rows, comments (`table_comment` and `column_comment` are returned verbatim, lines 477 and 482) and error texts flow into the model's context. Anyone who can write to the database can plant instructions there. No MCP server can fully fix this. It should be stated in the README, next to the masking caveat.

---

## Verdict

**Solid:**
- The layering idea: a least-privilege user plus a database-enforced read-only session, with layers 2 and 3 proven by the smoke test against a real writer account.
- The streaming cursor with real row, size and cell caps.
- The careful handling of stdout.
- Retry-once after a lost connection *for idle-death cases*.
- A test harness that drives real stdio JSON-RPC.

**Risky:**
- The pool, with its starving waiters (1) and reconnect-on-every-truncation (6).
- Hidden state on pooled connections (2).
- The timeout gap via `SHOW`, made worse by a blind retry that doubles runaway queries (3).
- Masking and the regex guard, which are easy to get past in several new ways (4, 8, 10) and will not port to other engines.
- Silent truncation of schema metadata (5).
- TLS that looks on but verifies nothing (7).

**Fix first:**
1. The pool's capacity signalling, and keeping connections after errors.
2. Always resetting the session database.
3. No retry after a read timeout, plus `KILL QUERY` on client timeout, and closing the `SHOW ... WHERE` hole.
4. Making `rows_of` truncation loud and capping the resource explicitly.
5. Explicit TLS modes.
6. Then replacing the regex guard with a parser (Phase 3), and adding a regression test for every finding above before its fix.

---

## Addendum (Phase 3): follow-up tests of what was left unverified

Each item below was tested after the review. Commands and outputs are in `spikes/`.

| Item | Result |
|---|---|
| Phase 1: stack on a non-default port | `SQL_MCP_MYSQL_PORT=33317 docker compose up -d --wait`, then the smoke test with the same variable: **56/56 PASS**. Moved back to 33316 afterwards. Side effect confirmed: `resolve_config()` wrote a second temp dir outside the project (`$TMPDIR/sqlmcp-gxr5thvi`). |
| Smoke flakiness under load (#21) | 56/56 PASS with 8 CPU-burning processes on a 12-core Mac. Not reproduced; Linux CI runners are still untested. |
| TLS with a CA (#7, #22) | Correct CA, `ssl_check_hostname: false`: connected, TLS 1.3, `CERT_REQUIRED`. Wrong CA: refused (`CERTIFICATE_VERIFY_FAILED ... self-signed certificate`). Correct CA with hostname check on 127.0.0.1: refused ("IP address mismatch"; MySQL's auto-generated certificate has no SAN). So the README's "(tested)" holds for the CA check, and hostname verification needs a properly issued certificate. There is still no automated test. |
| Reconnect cost on a network (#6) | Through Toxiproxy with 5 ms latency each way (~10 ms RTT): pooled `SELECT 1` 25.71 ms; non-truncated `LIMIT 10` 25.36 ms; **truncated 104.38 ms** (4x). |
| **#18 upgraded to Medium** | At ~10 ms RTT, the per-call `COM_INIT_DB` **doubles** latency: `SELECT 1` 25.45 ms with a default database vs 12.89 ms without. |
| #20 MariaDB | **CONFIRMED** (was SUSPECTED). Against MariaDB 11.4.13 every call fails: `MySQL error: (1193, "Unknown system variable 'max_execution_time'")`. MariaDB has `transaction_read_only` (11.1+) and `max_statement_time` (seconds). |
| **New, #24: optimizer-hint timeout override** (Medium, CONFIRMED) | As `readonly_user` with session `max_execution_time=1000`, `SELECT /*+ MAX_EXECUTION_TIME(60000) */ (heavy)` ran 2.5 s and returned `9372858`. Today's guard blocks it only because it rejects all comments. Any guard that tolerates comments (a parser that ignores them) must refuse hints explicitly. |
| Doc check of #3 (`FOR SHARE`) | The MySQL docs say a READ ONLY transaction prevents "modifying or locking" (`spikes/doc-verification.md` #3). Live, on 8.4.13 with the session setting and autocommit, `SELECT ... FOR SHARE` returned a row. The guard should refuse locking clauses either way. |
