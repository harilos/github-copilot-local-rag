from __future__ import annotations

import sys
import tempfile
import unittest
import unicodedata
from pathlib import Path


QUERY_ROOT = Path(__file__).resolve().parent
TOOL_ROOT = QUERY_ROOT.parent / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool import catalog
from software_rag_tool.retrieval import _matching_strong_exact_rows


class UnicodeFilenameExactContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.catalog_path = Path(self.temporary_directory.name) / "catalog.sqlite"
        with catalog.connect(self.catalog_path) as connection:
            for record_id, path in [
                ("unicode", "資料/Ｃａｆｅ́_ｶﾞｲﾄﾞ.DOCX"),
                ("japanese", "資料/運用手順書.pdf"),
                ("ascii", "docs/ReleaseNotes.TXT"),
            ]:
                catalog._insert_record(
                    connection,
                    {
                        "id": record_id,
                        "text": f"fixture body for {record_id}",
                        "metadata": {"path": path, "title": Path(path).stem},
                    },
                    "now",
                )
            connection.commit()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _assert_exact(self, query: str, expected_path: str) -> None:
        rows = catalog.exact_search(query, top_k=5, path=self.catalog_path)
        self.assertTrue(rows, query)
        self.assertEqual(expected_path, rows[0]["metadata"]["path"], query)
        self.assertIn("exact", rows[0]["signals"], query)
        self.assertEqual([rows[0]], _matching_strong_exact_rows(query, rows), query)

    def test_unicode_positive_six_of_six_are_exact_and_top_five(self) -> None:
        composed = unicodedata.normalize("NFC", "Ｃａｆｅ́_ｶﾞｲﾄﾞ.DOCX")
        decomposed = unicodedata.normalize("NFD", composed)
        cases = [
            ("Ｃａｆｅ́_ｶﾞｲﾄﾞ.DOCX", "資料/Ｃａｆｅ́_ｶﾞｲﾄﾞ.DOCX"),
            ("café_ガイド.docx", "資料/Ｃａｆｅ́_ｶﾞｲﾄﾞ.DOCX"),
            (decomposed, "資料/Ｃａｆｅ́_ｶﾞｲﾄﾞ.DOCX"),
            ("ＣＡＦÉ_ガイド", "資料/Ｃａｆｅ́_ｶﾞｲﾄﾞ.DOCX"),
            ("運用手順書.pdf", "資料/運用手順書.pdf"),
            ("運用手順書", "資料/運用手順書.pdf"),
        ]
        for query, expected_path in cases:
            with self.subTest(query=query):
                self._assert_exact(query, expected_path)

    def test_typo_and_partial_negative_zero_of_three_are_exact(self) -> None:
        for query in ["運用手順", "運用手順書誤", "café_ガイ.docx"]:
            with self.subTest(query=query):
                self.assertEqual(
                    [],
                    catalog.exact_search(query, top_k=5, path=self.catalog_path),
                )

    def test_title_only_unicode_match_is_not_verified_as_filename_exact(self) -> None:
        with catalog.connect(self.catalog_path) as connection:
            catalog._insert_record(
                connection,
                {
                    "id": "title-only",
                    "text": "運用手順書",
                    "metadata": {
                        "path": "docs/unrelated.txt",
                        "title": "運用手順書",
                    },
                },
                "now",
            )
            connection.commit()
        rows = catalog.exact_search(
            "運用手順書",
            top_k=5,
            path=self.catalog_path,
        )
        title_only = [row for row in rows if row["id"] == "title-only"]
        self.assertEqual(1, len(title_only))
        self.assertEqual(
            [],
            _matching_strong_exact_rows("運用手順書", title_only),
        )

    def test_ascii_four_of_four_remain_exact(self) -> None:
        for query in [
            "ReleaseNotes.TXT",
            "releasenotes.txt",
            "docs/ReleaseNotes.TXT",
            "docs/releasenotes.txt",
        ]:
            with self.subTest(query=query):
                self._assert_exact(query, "docs/ReleaseNotes.TXT")


if __name__ == "__main__":
    unittest.main()
