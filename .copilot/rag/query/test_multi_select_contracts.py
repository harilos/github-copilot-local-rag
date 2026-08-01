from __future__ import annotations

import sys
import unittest
from pathlib import Path

RAG_ROOT = Path(__file__).resolve().parents[1]
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

from multi_select import SelectionRow, safe_label, toggle_selection

class MultiSelectContractTests(unittest.TestCase):
    def choose(self, actions, count=5):
        pending = iter(actions)
        output = []
        invalid = []
        result = toggle_selection(
            tuple(SelectionRow(f"db-{i}", f"DB {i}") for i in range(1, count + 1)),
            ask=lambda _prompt: next(pending, None),
            output=output.append,
            invalid=invalid.append,
            title="DB selection",
        )
        return result, output, invalid

    def test_all_selection_is_frozen_as_explicit_keys(self):
        result, _output, invalid = self.choose(["c"], count=5)
        self.assertEqual("all", result.mode)
        self.assertEqual(("db-1", "db-2", "db-3", "db-4", "db-5"), result.keys)
        self.assertEqual([], invalid)

    def test_toggle_deduplicates_indexes_and_preserves_order(self):
        result, _output, invalid = self.choose(["1, 1, 3", "c"], count=5)
        self.assertEqual("explicit", result.mode)
        self.assertEqual(("db-2", "db-4", "db-5"), result.keys)
        self.assertEqual([], invalid)

    def test_all_none_cancel_and_eof_are_distinct(self):
        self.assertEqual("all", self.choose(["x", "a", "c"], 2)[0].mode)
        self.assertEqual("none", self.choose(["x", "c"], 2)[0].mode)
        self.assertEqual("cancelled", self.choose(["0"], 2)[0].mode)
        self.assertEqual("cancelled", self.choose([], 2)[0].mode)

    def test_invalid_tokens_never_change_selection(self):
        result, _output, invalid = self.choose(["", "1,", "z", "9", "c"], 2)
        self.assertEqual("all", result.mode)
        self.assertEqual(("db-1", "db-2"), result.keys)
        self.assertEqual(4, len(invalid))

    def test_duplicate_keys_and_terminal_controls_are_safe(self):
        with self.assertRaisesRegex(ValueError, "duplicated"):
            toggle_selection(
                (SelectionRow("Key", "one"), SelectionRow("key", "two")),
                ask=lambda _prompt: "c", output=lambda _value: None,
                invalid=lambda _value: None, title="duplicate",
            )
        self.assertEqual("red line", safe_label("\x1b[31mred\x1b[0m\nline"))

if __name__ == "__main__":
    unittest.main()
