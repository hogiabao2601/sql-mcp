"""Offline tests for result shaping and the schema overview cap (REVIEW #5)."""
import unittest

from sql_mcp.results import collect, format_overview


class OverviewCap(unittest.TestCase):
    def test_small_schema_is_complete_with_no_marker(self):
        text = format_overview({"orders": ["id int PK", "total decimal"], "items": ["id int PK"]}, 1000)
        self.assertEqual(text, "orders(id int PK, total decimal)\nitems(id int PK)")

    def test_big_schema_is_cut_at_a_table_boundary_and_says_how_much_is_missing(self):
        tables = {f"t{i:04d}": [f"c{j} varchar(255)" for j in range(20)] for i in range(1000)}
        text = format_overview(tables, 30_000)
        body, marker = text.rsplit("\n", 1)
        shown = len(body.splitlines())
        self.assertLessEqual(len(body), 30_000)
        self.assertEqual(marker, f"... {1000 - shown} more tables not shown (schema larger than 30000 chars); "
                                 "use list_tables / describe_table / search_schema.")
        self.assertTrue(all(line.endswith(")") for line in body.splitlines()))     # no table cut in half


class Collect(unittest.TestCase):
    def batches(self, rows):
        it = iter([rows[i:i + 200] for i in range(0, len(rows), 200)] + [[]])
        return lambda n: next(it)

    def test_custom_char_cap(self):
        rows, why = collect(self.batches([["x" * 100]] * 50), set(), 1000, max_chars=1_000)
        self.assertEqual((len(rows), why), (9, "size limit 1000 chars"))


if __name__ == "__main__":
    unittest.main()
