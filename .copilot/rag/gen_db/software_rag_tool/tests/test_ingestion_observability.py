from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from software_rag_tool import incremental
from software_rag_tool.embeddings import DocumentTokenBudget
from software_rag_tool.ingestion_paths import resolve_ingestion_scope


class _CharacterTokenizer:
    def __call__(self, text, **_kwargs):
        values = [text] if isinstance(text, str) else list(text)
        encoded = [[1, *range(2, len(value) + 2), 2] for value in values]
        return {"input_ids": encoded[0] if isinstance(text, str) else encoded}


TOKEN_BUDGET = DocumentTokenBudget(
    tokenizer=_CharacterTokenizer(),
    document_prefix="document: ",
    tokenizer_name="ingestion-observability-test-double",
    target_tokens=320,
    max_tokens=384,
)


class IngestionObservabilityTests(unittest.TestCase):
    def _run(self, root: Path, output: Path) -> dict:
        def records_for(_root: Path, path: Path, *_args, **_kwargs):
            if path.name == "broken.txt":
                raise ValueError("fixture extraction failure")
            if path.name == "empty.txt":
                return []
            return [{"id": path.name, "text": path.read_text(encoding="utf-8")}]

        with (
            mock.patch.dict(
                os.environ,
                {"RAG_OUTPUT_ROOT": str(output)},
                clear=False,
            ),
            mock.patch.object(incremental, "require_index_tokenizer"),
            mock.patch.object(incremental, "validate_existing_index_tokenizer"),
            mock.patch.object(
                incremental,
                "build_records_for_file",
                side_effect=records_for,
            ),
            mock.patch.object(incremental, "delete_ids", return_value=0),
            mock.patch.object(incremental, "delete_catalog_chunks", return_value=0),
            mock.patch.object(
                incremental,
                "upsert_records",
                side_effect=lambda records, **_kwargs: len(records),
            ),
            mock.patch.object(incremental, "upsert_catalog_records"),
            mock.patch.object(incremental, "collection_count", return_value=1),
            mock.patch.object(incremental, "write_manifest"),
            mock.patch.object(
                incremental,
                "update_profile_from_clean",
                return_value=False,
            ),
        ):
            return incremental.add_or_update_root(
                root,
                "src_fixture",
                document_token_budget=TOKEN_BUDGET,
            )

    def test_mixed_run_reports_three_distinct_categories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "documents"
            output = Path(temporary) / "db"
            root.mkdir()
            (root / "ok.txt").write_text("normal", encoding="utf-8")
            (root / "empty.txt").write_text("", encoding="utf-8")
            (root / "broken.txt").write_text("broken", encoding="utf-8")
            (root / "unsupported.bin").write_bytes(b"binary")

            result = self._run(root, output)
            state_path = output / "logs" / "index_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            broken = next(
                value
                for value in state["files"].values()
                if str(value.get("path") or "").endswith("broken.txt")
            )
            broken["content_hash"] = broken["failed_content_hash"]
            state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            repeated = self._run(root, output)

        diagnostics = result["ingestion_diagnostics"]
        self.assertEqual("failure", result["result_status"])
        self.assertEqual(2, result["indexed_files"])
        self.assertEqual(1, result["extract_error_files"])
        self.assertEqual(1, diagnostics["unsupported"]["count"])
        self.assertIn("unsupported.bin", diagnostics["unsupported"]["paths"][0])
        self.assertEqual(1, diagnostics["zero_text"]["count"])
        self.assertIn("empty.txt", diagnostics["zero_text"]["paths"][0])
        self.assertEqual(1, diagnostics["extraction_error"]["count"])
        self.assertIn("broken.txt", diagnostics["extraction_error"]["paths"][0])
        repeated_diagnostics = repeated["ingestion_diagnostics"]
        self.assertEqual(1, repeated_diagnostics["unsupported"]["count"])
        self.assertEqual(1, repeated_diagnostics["zero_text"]["count"])
        self.assertIn("empty.txt", repeated_diagnostics["zero_text"]["paths"][0])
        self.assertEqual(1, repeated_diagnostics["extraction_error"]["count"])
        self.assertIn(
            "broken.txt",
            repeated_diagnostics["extraction_error"]["paths"][0],
        )

    def test_clean_run_has_zero_diagnostics_and_ignores_vcs_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "documents"
            output = Path(temporary) / "db"
            (root / ".git").mkdir(parents=True)
            (root / "ok.txt").write_text("normal", encoding="utf-8")
            (root / ".git" / "config").write_text("internal", encoding="utf-8")

            result = self._run(root, output)

        self.assertEqual("success", result["result_status"])
        for category in result["ingestion_diagnostics"].values():
            self.assertEqual(0, category["count"])
            self.assertEqual([], category["paths"])

    def test_document_only_selection_does_not_mislabel_supported_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "guide.md").write_text("guide", encoding="utf-8")
            (root / "script.py").write_text("print('ok')", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"LOCAL_RAG_FILE_SELECTION": "documents_only"},
                clear=False,
            ):
                paths = incremental._unsupported_input_paths(
                    resolve_ingestion_scope(root)
                )
        self.assertEqual([], paths)


if __name__ == "__main__":
    unittest.main()
