"""Offline tests for identifier quoting (REVIEW #12)."""
import unittest

from sql_mcp.config import _profile
from sql_mcp.dialects.mysql import MySQL


class Quoting(unittest.TestCase):
    def test_mysql_quotes_and_escapes_backticks(self):
        d = MySQL(_profile("x", {"user": "u"}))
        self.assertEqual(d.quote("order-archive"), "`order-archive`")
        self.assertEqual(d.quote("we`ird"), "`we``ird`")
        self.assertEqual(d.quote("café"), "`café`")



class MariaDBSession(unittest.TestCase):
    def test_version_picks_the_read_only_variable_and_seconds(self):
        from sql_mcp.dialects.mariadb import MariaDB, server_version

        class Raw:
            def __init__(self, v):
                self.server_version = v
        d = MariaDB(_profile("x", {"user": "u", "engine": "mariadb", "timeout_ms": 1500}))
        self.assertEqual(server_version(Raw("11.4.13-MariaDB-ubu2404")), (11, 4))
        self.assertEqual(server_version(Raw("5.5.5-10.11.19-MariaDB")), (10, 11))
        self.assertIn("SESSION tx_read_only=1", d.session_sql((10, 11)))
        self.assertIn("SESSION transaction_read_only=1", d.session_sql((11, 1)))
        self.assertIn("max_statement_time=1.5,", d.session_sql((11, 4)))


if __name__ == "__main__":
    unittest.main()
