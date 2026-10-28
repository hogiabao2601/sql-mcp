"""Offline tests for profile parsing and validation (REVIEW #9)."""
import unittest

from mcp.server.mcpserver.exceptions import ToolError

from sql_mcp.config import _profile


class ConfigTests(unittest.TestCase):
    def test_defaults_to_mysql_with_old_keys(self):
        p = _profile("x", {"user": "u", "password": "pw", "database": "shop", "ssl_ca": "/ca.pem",
                           "ssl_check_hostname": False, "mask_columns": "a, b"})
        self.assertEqual((p.engine, p.port, p.timeout_ms, p.pool_size, p.mask), ("mysql", 3306, 5000, 3, ("a", "b")))

    def test_timeout_zero_is_refused(self):           # 0 means "no limit" on MySQL
        with self.assertRaisesRegex(ToolError, "'timeout_ms' must be between 100 and 600000, got 0"):
            _profile("x", {"user": "u", "timeout_ms": 0})

    def test_pool_size_zero_is_refused(self):         # made every call wait 10 s, then fail
        with self.assertRaisesRegex(ToolError, "'pool_size' must be between 1 and 20, got 0"):
            _profile("x", {"user": "u", "pool_size": 0})

    def test_non_integer_is_refused(self):
        with self.assertRaisesRegex(ToolError, "'timeout_ms' must be an integer"):
            _profile("x", {"user": "u", "timeout_ms": "5s"})

    def test_unknown_key_is_refused(self):
        with self.assertRaisesRegex(ToolError, "unknown key\\(s\\) timout_ms"):
            _profile("x", {"user": "u", "timout_ms": 100})

    def test_unknown_engine_is_refused(self):
        with self.assertRaisesRegex(ToolError, "engine must be one of"):
            _profile("x", {"user": "u", "engine": "oracle"})

    def test_sqlite_files(self):
        p = _profile("x", {"engine": "sqlite", "databases": {"shop": "/a.db", "analytics": "/b.db"}})
        self.assertEqual((p.databases, p.database), ((("shop", "/a.db"), ("analytics", "/b.db")), "shop"))
        self.assertEqual(_profile("x", {"engine": "sqlite", "path": "/a.db"}).databases, (("main", "/a.db"),))
        for bad in [{"path": "/a.db", "databases": {"a": "/a.db"}}, {"databases": {}},
                    {"databases": {f"d{i}": "/x.db" for i in range(10)}}, {"databases": {"a": "/a.db"}, "database": "b"}]:
            with self.subTest(bad=bad), self.assertRaises(ToolError):
                _profile("x", dict(engine="sqlite", **bad))

    def test_sqlite_needs_path_not_user(self):
        self.assertEqual(_profile("x", {"engine": "sqlite", "path": "/tmp/a.db"}).path, "/tmp/a.db")
        with self.assertRaisesRegex(ToolError, "'path'"):
            _profile("x", {"engine": "sqlite"})



class TlsConfigTests(unittest.TestCase):          # REVIEW #7: explicit TLS modes
    def mode(self, **raw):
        p = _profile("x", dict(user="u", **raw))
        return p.tls, p.tls_ca

    def test_legacy_keys_map_to_modes(self):
        self.assertEqual(self.mode(), ("prefer", None))                                   # today's default
        self.assertEqual(self.mode(ssl_ca="/ca.pem"), ("verify-full", "/ca.pem"))
        self.assertEqual(self.mode(ssl_ca="/ca.pem", ssl_check_hostname=False), ("verify-ca", "/ca.pem"))

    def test_explicit_modes(self):
        for m in ("disable", "prefer", "require"):
            self.assertEqual(self.mode(tls=m), (m, None))
        self.assertEqual(self.mode(tls="verify-full", tls_ca="/ca.pem"), ("verify-full", "/ca.pem"))

    def test_verify_modes_need_a_ca(self):
        for m in ("verify-ca", "verify-full"):
            with self.subTest(m=m), self.assertRaisesRegex(ToolError, "needs 'tls_ca'"):
                self.mode(tls=m)

    def test_unknown_mode_and_mixed_keys_are_refused(self):
        with self.assertRaisesRegex(ToolError, "tls must be one of"):
            self.mode(tls="on")
        with self.assertRaisesRegex(ToolError, "either"):
            self.mode(tls="require", ssl_ca="/ca.pem")



class PasswordCmd(unittest.TestCase):          # REVIEW #15
    def p(self, **raw):
        return _profile("x", dict(user="u", **raw))

    def test_stdout_is_the_password_without_the_trailing_newline(self):
        import sys
        self.assertEqual(self.p(password_cmd=[sys.executable, "-c", "print('s3cret ')"]).password, "s3cret ")

    def test_must_be_an_argv_list_never_a_shell_string(self):
        with self.assertRaisesRegex(ToolError, "list"):
            self.p(password_cmd="echo s3cret")

    def test_failure_reports_exit_code_and_stderr_but_never_stdout(self):
        import sys
        cmd = [sys.executable, "-c", "import sys; print('LEAKED'); print('item not found', file=sys.stderr); sys.exit(3)"]
        with self.assertRaises(ToolError) as cm:
            self.p(password_cmd=cmd)
        self.assertIn("exit code 3", str(cm.exception))
        self.assertIn("item not found", str(cm.exception))
        self.assertNotIn("LEAKED", str(cm.exception))

    def test_timeout(self):
        import sys
        from sql_mcp import config as cfg
        old, cfg.PASSWORD_CMD_TIMEOUT_S = cfg.PASSWORD_CMD_TIMEOUT_S, 0.5
        try:
            with self.assertRaisesRegex(ToolError, "did not finish"):
                self.p(password_cmd=[sys.executable, "-c", "import time; time.sleep(5)"])
        finally:
            cfg.PASSWORD_CMD_TIMEOUT_S = old

    def test_only_one_password_source(self):
        with self.assertRaisesRegex(ToolError, "only one of"):
            self.p(password="a", password_env="B")


class Reload(unittest.TestCase):          # REVIEW #14: profile file changes apply without a restart
    def test_changed_file_is_reloaded_and_old_pools_closed(self):
        import json
        import os
        import pathlib
        from sql_mcp import config as cfg
        path = pathlib.Path(__file__).resolve().parents[2] / ".smoke" / "reload.json"
        path.write_text(json.dumps({"profiles": {"a": {"user": "u", "timeout_ms": 1000}}}))
        closed = []
        cfg.on_reload(lambda: closed.append(1))
        old = os.environ.get("SQL_MCP_CONFIG")
        os.environ["SQL_MCP_CONFIG"] = str(path)
        try:
            cfg.config.cache_clear()
            self.assertEqual(cfg.get_profile("a").timeout_ms, 1000)
            path.write_text(json.dumps({"profiles": {"a": {"user": "u", "timeout_ms": 2000}}}))
            os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000_000))
            self.assertEqual(cfg.get_profile("a").timeout_ms, 2000)
            self.assertEqual(closed, [1])
        finally:
            if old is None:
                os.environ.pop("SQL_MCP_CONFIG", None)
            else:
                os.environ["SQL_MCP_CONFIG"] = old
            cfg.config.cache_clear()


if __name__ == "__main__":
    unittest.main()
