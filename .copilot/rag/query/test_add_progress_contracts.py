from __future__ import annotations

import unittest
from pathlib import Path


RAG_ROOT = Path(__file__).resolve().parents[1]
ADD_DATA = RAG_ROOT / "gen_db" / "add_data.py"
PROGRESS_RENDERER = RAG_ROOT / "source_manager" / "progress.py"


class AddProgressContracts(unittest.TestCase):
    def test_add_cli_emits_total_current_file_and_remaining_time(self) -> None:
        text = ADD_DATA.read_text(encoding="utf-8")
        self.assertIn("class _AddProgressWatcher", text)
        self.assertIn("def _install_exact_file_index_progress", text)
        self.assertIn('updates.setdefault("current_file_index", current_index)', text)
        self.assertIn('"event": "add.file_progress"', text)
        self.assertIn('"current_index": current_index', text)
        self.assertIn('"total_kind": total_kind', text)
        self.assertIn('total_kind = "exact"', text)
        self.assertIn('total_kind = "estimated"', text)
        self.assertIn('"eta_seconds"', text)
        self.assertIn('"remaining_seconds_min"', text)
        self.assertIn('"remaining_seconds_max"', text)
        self.assertIn('db_root / "logs" / "progress.json"', text)
        self.assertIn("preflight_estimated_documents", text)

    def test_renderer_keeps_required_japanese_progress_phrasing(self) -> None:
        text = PROGRESS_RENDERER.read_text(encoding="utf-8")
        self.assertIn("全{total:,}件中", text)
        self.assertIn("今{min(total, current_index):,}ファイル目", text)
        self.assertIn("残り約", text)
        self.assertIn("残り目安約", text)


if __name__ == "__main__":
    unittest.main()
