"""Offline tests for the connection pool (no database)."""
import threading
import time
import unittest

from mcp.server.mcpserver.exceptions import ToolError

from sql_mcp import pool as poolmod
from sql_mcp.pool import Conn, Pool


class Fake:
    def __init__(self, alive=True):
        self.alive, self.closed = alive, False

    def close(self):
        self.closed = True


def make(size=1):
    opened = []

    def open_():
        c = Conn(Fake())
        opened.append(c)
        return c

    def ping(raw):
        if not raw.alive:
            raise ConnectionError("dead")
    return Pool("t", size, open_, ping, lambda raw: raw.close()), opened


class PoolTests(unittest.TestCase):
    def _waiter_after(self, pool, holder_action):
        held = pool.acquire()
        got = {}

        def waiter():
            t0 = time.monotonic()
            try:
                pool.acquire()
                got["ok"] = time.monotonic() - t0
            except ToolError as e:
                got["err"] = str(e)
        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.2)
        holder_action(held)
        t.join(timeout=12)
        return got

    def test_waiter_wakes_when_busy_connection_is_released(self):
        pool, _ = make(1)
        got = self._waiter_after(pool, pool.release)
        self.assertIn("ok", got)
        self.assertLess(got["ok"], 1.0)

    def test_waiter_wakes_when_busy_connection_is_discarded(self):     # REVIEW #1
        pool, opened = make(1)
        got = self._waiter_after(pool, pool.discard)
        self.assertIn("ok", got, got)
        self.assertLess(got["ok"], 1.0)
        self.assertEqual(len(opened), 2)          # the waiter got a NEW connection in the freed slot

    def test_busy_pool_times_out_with_readable_error(self):
        pool, _ = make(1)
        pool.acquire()
        old = poolmod.WAIT_S
        poolmod.WAIT_S = 0.3
        try:
            with self.assertRaisesRegex(ToolError, "All 1 connections of profile 't' are busy"):
                pool.acquire()
        finally:
            poolmod.WAIT_S = old

    def test_dead_idle_connection_is_replaced(self):
        pool, opened = make(1)
        c = pool.acquire()
        pool.release(c)
        c.raw.alive, c.last_used = False, time.monotonic() - poolmod.IDLE_PING_S - 1
        c2 = pool.acquire()
        self.assertIsNot(c2, c)
        self.assertTrue(c.raw.closed)
        self.assertEqual(len(opened), 2)

    def test_old_connection_is_recycled(self):
        pool, opened = make(1)
        c = pool.acquire()
        pool.release(c)
        c.born = time.monotonic() - poolmod.MAX_LIFETIME_S - 1
        self.assertIsNot(pool.acquire(), c)
        self.assertTrue(c.raw.closed)

    def test_failed_open_frees_the_slot(self):
        calls = []

        def open_():
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionError("refused")
            return Conn(Fake())
        pool = Pool("t", 1, open_, lambda raw: None, lambda raw: None)
        with self.assertRaises(ConnectionError):
            pool.acquire()
        self.assertIsInstance(pool.acquire(), Conn)


if __name__ == "__main__":
    unittest.main()
