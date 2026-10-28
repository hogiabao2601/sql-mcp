"""Offline tests for masking by SOURCE column (REVIEW #4). No database: the schema is given."""
import unittest

from mcp.server.mcpserver.exceptions import ToolError

from sql_mcp.guard import check
from sql_mcp.masking import plan

SCHEMA = {"analytics": {"app_users": {"id": "int", "email": "text", "password_hash": "text", "api_token": "text",
                                      "created_at": "datetime"},
                        "page_views": {"id": "int", "user_id": "int", "path": "text"}},
          "shop": {"orders": {"id": "int", "customer_id": "int", "status": "text", "total": "decimal"}}}
MASK = ("password", "token", "email")


def masked_outputs(sql, db="analytics"):
    m = plan(check(sql, "mysql"), "mysql", db, SCHEMA, MASK)
    return sorted(m.columns), m.reads_masked


class MaskingTests(unittest.TestCase):
    def test_direct_and_aliased_columns_are_masked(self):
        self.assertEqual(masked_outputs("SELECT id, api_token FROM app_users"), ([1], True))
        self.assertEqual(masked_outputs("SELECT api_token AS x FROM app_users"), ([0], True))

    def test_star_expands_to_the_masked_columns(self):
        self.assertEqual(masked_outputs("SELECT * FROM app_users"), ([1, 2, 3], True))

    def test_expressions_union_and_derived_tables_are_masked(self):   # all leaked before
        self.assertEqual(masked_outputs("SELECT CONCAT(api_token, '') AS x FROM app_users")[0], [0])
        self.assertEqual(masked_outputs("SELECT 'n/a' AS x UNION ALL SELECT api_token FROM app_users")[0], [0])
        self.assertEqual(masked_outputs("SELECT t.x FROM (SELECT api_token AS x FROM app_users) t")[0], [0])
        self.assertEqual(masked_outputs("WITH c AS (SELECT email AS e FROM app_users) SELECT UPPER(e) AS u FROM c")[0], [0])

    def test_masked_columns_outside_the_select_list_are_refused(self):    # boolean oracles
        for sql in ["SELECT id FROM app_users WHERE api_token LIKE 'tok_a%'",
                    "SELECT id FROM app_users ORDER BY password_hash",
                    "SELECT COUNT(*) FROM app_users GROUP BY email",
                    "SELECT u.id FROM app_users u JOIN page_views v ON v.path = u.email",
                    "SELECT t.id FROM (SELECT id, api_token AS x FROM app_users) t WHERE t.x > 'a'"]:
            with self.subTest(sql=sql), self.assertRaisesRegex(ToolError, "masked column"):
                masked_outputs(sql)

    def test_unrelated_queries_are_untouched(self):
        self.assertEqual(masked_outputs("SELECT id, created_at FROM app_users"), ([], False))
        self.assertEqual(masked_outputs("SELECT u.id, COUNT(*) FROM app_users u JOIN page_views v "
                                        "ON v.user_id = u.id GROUP BY u.id"), ([], False))
        self.assertEqual(masked_outputs("SELECT o.status, COUNT(*) FROM shop.orders o JOIN analytics.app_users u "
                                        "ON u.id = o.customer_id GROUP BY o.status", db="shop"), ([], False))

    def test_error_channel_queries_are_flagged(self):          # EXTRACTVALUE leaks through the error text
        cols, reads = masked_outputs("SELECT EXTRACTVALUE(1, CONCAT(0x7e, (SELECT api_token FROM app_users LIMIT 1))) AS x")
        self.assertEqual((cols, reads), ([0], True))

    def test_unknown_columns_fall_back_to_names(self):
        m = plan(check("SELECT nope FROM app_users", "mysql"), "mysql", "analytics", SCHEMA, MASK)
        self.assertEqual(m.columns, set())


if __name__ == "__main__":
    unittest.main()
