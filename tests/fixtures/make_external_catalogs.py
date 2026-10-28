"""Create StarRocks external catalogs for tests/conformance/starrocks_external.py, backed by the S3 and
catalog containers in docker/starrocks.compose.yml (profile "ext"). Idempotent. TEST-ONLY credentials.

    docker compose -f docker/starrocks.compose.yml --profile ext up -d --wait
    .venv/bin/python tests/fixtures/make_external_catalogs.py [iceberg|hive|all]

Each catalog gets a database `lake` with a table `events(id, kind, api_token, at)` and three rows, plus a
SELECT grant for readonly_user. `api_token` is a secret-looking column, to test masking on external tables.
"""
from __future__ import annotations

import sys

import pymysql

S3 = ("'aws.s3.endpoint' = 'http://seaweedfs:8333', 'aws.s3.enable_path_style_access' = 'true', "
      "'aws.s3.access_key' = 'sqlmcp-lake', 'aws.s3.secret_key' = 'sqlmcp-lake-secret-test-only', "
      "'aws.s3.region' = 'us-east-1'")
ROWS = ("(1, 'login', 'tok_lake_aaaaaaaaaaaaaaaa', now()), (2, 'view', 'tok_lake_bbbbbbbbbbbbbbbb', now()), "
        "(3, 'buy', 'tok_lake_cccccccccccccccc', now())")
CATALOGS = {
    "iceberg_cat": (f"CREATE EXTERNAL CATALOG iceberg_cat PROPERTIES ('type' = 'iceberg', "
                    f"'iceberg.catalog.type' = 'rest', 'iceberg.catalog.uri' = 'http://iceberg-rest:8181', "
                    f"'iceberg.catalog.warehouse' = 'warehouse', {S3})"),
    "hive_cat": (f"CREATE EXTERNAL CATALOG hive_cat PROPERTIES ('type' = 'hive', "
                 f"'hive.metastore.uris' = 'thrift://hive-metastore:9083', {S3})"),
}


def conn():
    return pymysql.connect(host="127.0.0.1", port=39030, user="root", password="", autocommit=True)


def run(cur, sql):
    cur.execute(sql)
    return cur.fetchall() if cur.description else None


def build(which: str) -> None:
    c = conn()
    with c.cursor() as cur:
        cur.execute("SHOW CATALOGS")
        existing = {r[0] for r in cur.fetchall()}
        if which not in existing:
            run(cur, CATALOGS[which])
        run(cur, f"CREATE DATABASE IF NOT EXISTS {which}.lake")
        run(cur, f"CREATE TABLE IF NOT EXISTS {which}.lake.events "
                 "(id INT, kind STRING, api_token STRING, at DATETIME)")
        if run(cur, f"SELECT COUNT(*) FROM {which}.lake.events")[0][0] == 0:
            run(cur, f"INSERT INTO {which}.lake.events VALUES {ROWS}")
        run(cur, f"GRANT USAGE ON CATALOG {which} TO USER readonly_user")
        run(cur, f"SET CATALOG {which}")
        run(cur, "GRANT SELECT ON ALL TABLES IN DATABASE lake TO USER readonly_user")
        run(cur, "SET CATALOG default_catalog")
        print(f"{which}: ready ({run(cur, f'SELECT COUNT(*) FROM {which}.lake.events')[0][0]} rows)")
    c.close()


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    names = list(CATALOGS) if target == "all" else [t for t in CATALOGS if t.startswith(target)]
    for name in names:
        try:
            build(name)
        except Exception as e:
            print(f"{name}: SKIPPED ({str(e).splitlines()[0][:120]})")
