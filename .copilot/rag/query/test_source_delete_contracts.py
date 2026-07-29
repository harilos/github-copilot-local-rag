from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


TOOL_ROOT = (
    Path(__file__).resolve().parents[1]
    / "gen_db"
    / "software_rag_tool"
)
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool import source_delete  # noqa: E402
from software_rag_tool import catalog  # noqa: E402


class SourceDeleteContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-source-delete-"
        )
        self.root = Path(self.temporary.name)
        self.clean = self.root / "data" / "clean"
        self.logs = self.root / "logs"
        self.index = self.root / "index"
        (self.clean / "records").mkdir(parents=True)
        self.logs.mkdir()
        self.index.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _record(source_id: str, chunk_id: str) -> dict[str, object]:
        return {
            "id": chunk_id,
            "text": "fixture",
            "metadata": {
                "source_id": source_id,
                "path": f"{source_id}/document.md",
            },
        }

    def test_exact_source_delete_preserves_sibling_clean_and_state(self) -> None:
        source_a = self.clean / "records" / "a.jsonl"
        source_b = self.clean / "records" / "b.jsonl"
        source_a.write_text(
            json.dumps(self._record("source-a", "a-chunk")) + "\n",
            encoding="utf-8",
        )
        source_b.write_text(
            json.dumps(self._record("source-b", "b-chunk")) + "\n",
            encoding="utf-8",
        )
        (self.logs / "index_state.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "files": {
                        "source-a:file": {
                            "source_id": "source-a",
                            "record_ids": ["a-chunk"],
                        },
                        "source-b:file": {
                            "source_id": "source-b",
                            "record_ids": ["b-chunk"],
                        },
                    },
                    "ingestion": {"source_id": "source-a"},
                }
            ),
            encoding="utf-8",
        )
        (self.index / "manifest.json").write_text(
            json.dumps({"record_count": 2, "collection": "fixture"}),
            encoding="utf-8",
        )
        with (
            mock.patch.object(source_delete, "clean_dir", return_value=self.clean),
            mock.patch.object(source_delete, "logs_dir", return_value=self.logs),
            mock.patch.object(source_delete, "index_dir", return_value=self.index),
            mock.patch.object(
                source_delete.catalog,
                "source_chunk_ids",
                return_value=["a-chunk"],
            ),
            mock.patch.object(
                source_delete.catalog,
                "delete_source_documents",
                return_value={"documents": 1, "chunks": 1},
            ),
            mock.patch.object(
                source_delete.catalog,
                "counts",
                return_value={"chunks": 1},
            ),
            mock.patch.object(
                source_delete,
                "delete_ids",
                return_value=1,
            ) as delete_ids,
        ):
            result = source_delete.delete_source_data("source-a")

        self.assertEqual("deleted", result["status"])
        self.assertFalse(source_a.exists())
        self.assertTrue(source_b.exists())
        remaining = json.loads(
            (self.logs / "index_state.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("source-a:file", remaining["files"])
        self.assertIn("source-b:file", remaining["files"])
        self.assertEqual({}, remaining["ingestion"])
        delete_ids.assert_called_once_with(["a-chunk"])
        manifest = json.loads(
            (self.index / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(1, manifest["record_count"])

    def test_catalog_delete_uses_exact_source_id(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"RAG_OUTPUT_ROOT": str(self.root)},
        ):
            catalog.upsert_records(
                [
                    {
                        "id": "a-chunk",
                        "text": "A",
                        "metadata": {
                            "doc_id": "doc-a",
                            "source_id": "source-a",
                            "path": "same/document.md",
                        },
                    },
                    {
                        "id": "b-chunk",
                        "text": "B",
                        "metadata": {
                            "doc_id": "doc-b",
                            "source_id": "source-b",
                            "path": "same/document.md",
                        },
                    },
                ]
            )
            self.assertEqual(
                ["a-chunk"],
                catalog.source_chunk_ids("source-a"),
            )
            deleted = catalog.delete_source_documents("source-a")
            self.assertEqual({"documents": 1, "chunks": 1}, deleted)
            self.assertEqual([], catalog.source_chunk_ids("source-a"))
            self.assertEqual(
                ["b-chunk"],
                catalog.source_chunk_ids("source-b"),
            )


if __name__ == "__main__":
    unittest.main()
