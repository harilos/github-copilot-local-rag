from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from software_rag_tool import file_selection, incremental
from software_rag_tool.document_extensions import (
    FILE_SELECTION_ALL,
    FILE_SELECTION_DOCUMENTS,
    FILE_SELECTION_ENV,
)
from test_rebuild_scope_authority import TOKEN_BUDGET


class SharePointFolderSelectionTests(unittest.TestCase):
    """Keep filesystem discovery, extraction, checkpoints and cleanup real."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "sharepoint"
        self.root.mkdir()
        self.output = self.base / "db"
        self.state_path = self.output / "logs" / "index_state.json"
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.dict(os.environ, {
            "RAG_OUTPUT_ROOT": str(self.output), FILE_SELECTION_ENV: FILE_SELECTION_ALL,
        }))
        stack.enter_context(redirect_stdout(io.StringIO()))
        for name in (
            "require_index_tokenizer", "validate_existing_index_tokenizer",
            "write_manifest", "emit_event", "reset_collection", "reset_catalog",
        ):
            setattr(self, name, stack.enter_context(mock.patch.object(incremental, name)))
        self.progress = stack.enter_context(mock.patch.object(incremental, "write_progress"))
        stack.enter_context(mock.patch.object(incremental, "update_profile_from_clean", return_value=False))
        stack.enter_context(mock.patch.object(incremental, "collection_count", return_value=1))
        self.records = stack.enter_context(mock.patch.object(
            incremental, "build_records_for_file", wraps=incremental.build_records_for_file,
        ))
        self.upsert = stack.enter_context(mock.patch.object(
            incremental, "upsert_records", side_effect=lambda values, **_kwargs: len(values),
        ))
        self.delete = stack.enter_context(mock.patch.object(
            incremental, "delete_ids", side_effect=len,
        ))
        self.catalog = stack.enter_context(mock.patch.object(incremental, "upsert_catalog_records"))
        self.catalog_delete = stack.enter_context(mock.patch.object(
            incremental, "delete_catalog_chunks", side_effect=len,
        ))

    def write(self, relative: str, text: str = "fixture document") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run_add(self, **overrides):
        return incremental.add_or_update_root(**{
            "root": self.root,
            "source_id": "sharepoint-source",
            "scan_subdir": ".",
            "batch_size_files": 3,
            "chunk_max_chars": 900,
            "chunk_overlap": 0,
            "document_token_budget": TOKEN_BUDGET,
            **overrides,
        })

    def state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def source_entries(self, source_id="sharepoint-source"):
        return {
            value["path"]: value for value in self.state()["files"].values()
            if value["source_id"] == source_id
        }

    def output_snapshot(self):
        return {
            path.relative_to(self.output).as_posix(): path.read_bytes()
            for path in self.output.rglob("*") if path.is_file()
        }

    def test_selected_folders_prune_body_reads_and_unsupported_diagnostics(self):
        expected = {"docs/spec/readme.md", "manuals/guide.txt"}
        for relative in sorted(expected | {
            "docs/spec/archive/old.txt", "docs/spec/draft.tmp",
            "docs/private.txt", "private/secret.txt", "private/unknown.unsupported",
        }):
            self.write(relative)
        real_open = Path.open
        opened = set()

        def guarded_open(path, mode="r", *args, **kwargs):
            if path.is_relative_to(self.root):
                relative = path.relative_to(self.root).as_posix()
                self.assertIn(relative, expected, f"unselected body opened: {relative}")
                opened.add(relative)
            return real_open(path, mode, *args, **kwargs)

        with mock.patch.object(Path, "open", guarded_open):
            result = self.run_add(
                include_paths=["docs/spec", "manuals", "docs/spec"],
                exclude_paths=["**/archive/**", "**/*.tmp"],
            )
        self.assertEqual("success", result["result_status"])
        self.assertEqual(2, result["indexed_files"])
        self.assertEqual(expected, opened)
        self.assertEqual(0, result["ingestion_diagnostics"]["unsupported"]["count"])
        self.assertEqual({f"sharepoint/{path}" for path in expected}, set(self.source_entries()))
        saved = self.state()["ingestion"]
        self.assertEqual(["docs/spec", "manuals"], saved["include_paths"])
        self.assertEqual(["**/archive/**", "**/*.tmp"], saved["exclude_paths"])
        self.assertEqual(".", saved["scan_subdir"])

    def test_shrink_expand_exclude_all_and_restore_preserve_other_source(self):
        for relative in ("docs/a.txt", "manuals/b.txt", "private/c.txt"):
            self.write(relative, relative)
        self.run_add(source_id="other-source")
        other_entries = self.source_entries("other-source")
        other_ids = {record for entry in other_entries.values() for record in entry["record_ids"]}
        other_clean = {
            path: (self.output / "data" / "clean" / entry["records_path"]).read_bytes()
            for path, entry in other_entries.items()
        }
        self.assertEqual(3, self.run_add()["indexed_files"])
        phases = (
            ({"include_paths": ["docs"]}, 1, 0, 1, 2),
            ({"include_paths": ["docs", "manuals"]}, 2, 1, 1, 0),
            ({"include_paths": ["docs", "manuals"], "exclude_paths": ["docs", "manuals"]}, 0, 0, 0, 2),
            ({}, 3, 3, 0, 0),
        )
        for arguments, count, indexed, skipped, deleted in phases:
            with self.subTest(arguments=arguments):
                previous = self.source_entries()
                self.delete.reset_mock()
                self.catalog_delete.reset_mock()
                result = self.run_add(**arguments)
                self.assertEqual("success", result["result_status"])
                self.assertEqual((count, indexed, skipped, deleted), (
                    result["file_count"], result["indexed_files"], result["skipped_files"], result["deleted_files"],
                ))
                current = self.source_entries()
                removed = set(previous) - set(current)
                removed_ids = {record for path in removed for record in previous[path]["record_ids"]}
                vector_deleted = {record for call in self.delete.call_args_list for record in call.args[0]}
                catalog_deleted = {record for call in self.catalog_delete.call_args_list for record in call.args[0]}
                self.assertEqual(removed_ids, vector_deleted)
                self.assertEqual(removed_ids, catalog_deleted)
                self.assertTrue(other_ids.isdisjoint(vector_deleted))
                for path in removed:
                    self.assertFalse((self.output / "data" / "clean" / previous[path]["records_path"]).exists())
                self.assertEqual(other_entries, self.source_entries("other-source"))
                for path, entry in other_entries.items():
                    self.assertEqual(other_clean[path], (self.output / "data" / "clean" / entry["records_path"]).read_bytes())
                self.assertEqual(3, len(list(self.root.rglob("*.txt"))))

    def test_resume_keeps_selection_and_rejects_changes_before_writes(self):
        self.write("docs/a.txt")
        self.write("manuals/b.txt")
        saved = {"include_paths": ["docs", "manuals"], "exclude_paths": ["**/drafts/**"]}
        self.run_add(**saved)
        resumed = self.run_add(resume=True, **saved)
        self.assertEqual(2, resumed["skipped_files"])
        self.assertEqual(0, resumed["indexed_files"])
        for field, value in (("include_paths", ["docs"]), ("exclude_paths", [])):
            with self.subTest(field=field):
                before = self.output_snapshot()
                actions = (self.progress, self.records, self.upsert, self.delete, self.catalog, self.catalog_delete)
                for action in actions:
                    action.reset_mock()
                with (
                    mock.patch.object(incremental, "_save_state", wraps=incremental._save_state) as save,
                    self.assertRaisesRegex(ValueError, field),
                ):
                    self.run_add(resume=True, **{**saved, field: value})
                save.assert_not_called()
                for action in actions:
                    action.assert_not_called()
                self.assertEqual(before, self.output_snapshot())

    def test_invalid_selected_folder_rejected_before_requested_reset(self):
        self.write("docs/a.txt")
        self.run_add()
        for selection in (["missing"], ["../outside"], ["/absolute"], ["docs/*.txt"], ["docs/a.txt"]):
            with self.subTest(selection=selection):
                before = self.output_snapshot()
                for action in (self.reset_collection, self.reset_catalog):
                    action.reset_mock()
                with (
                    mock.patch.object(incremental, "_reset_clean_dir") as reset_clean,
                    mock.patch.object(incremental, "_save_state") as save,
                    self.assertRaises(ValueError),
                ):
                    self.run_add(include_paths=selection, reset_db=True, reset_clean=True)
                for action in (self.reset_collection, self.reset_catalog, reset_clean, save):
                    action.assert_not_called()
                self.assertEqual(before, self.output_snapshot())

    def test_documents_only_still_applies_with_folder_and_glob_selection(self):
        for relative in ("docs/a.txt", "docs/model.puml", "docs/script.py", "docs/draft.md", "other/guide.txt"):
            self.write(relative)
        with mock.patch.dict(os.environ, {FILE_SELECTION_ENV: FILE_SELECTION_DOCUMENTS}):
            result = self.run_add(include_paths=["docs"], exclude_paths=["**/draft.*"])
        self.assertEqual("success", result["result_status"])
        self.assertEqual(2, result["indexed_files"])
        self.assertEqual({"sharepoint/docs/a.txt", "sharepoint/docs/model.puml"}, set(self.source_entries()))
        self.assertEqual(2, self.records.call_count)

    def test_saved_scope_and_resume_commands_retain_filters(self):
        from software_rag_tool.ingestion_paths import validated_saved_ingestion
        import importlib.util
        self.write("docs/a.txt")
        self.run_add(include_paths=["docs"], exclude_paths=["docs/archive"])
        state = self.state()
        scope = validated_saved_ingestion(state)
        self.assertEqual(["docs"], scope["include_paths"])
        self.assertEqual(["docs/archive"], scope["exclude_paths"])
        path = Path(__file__).resolve().parents[2] / "status.py"
        spec = importlib.util.spec_from_file_location("selection_status_test", path)
        status = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(status)
        command = ["python", "add_data.py"]
        status._append_saved_options(command, scope)
        self.assertIn("--include-path=docs", command)
        self.assertIn("--exclude-path=docs/archive", command)
        state["ingestion"]["include_paths"] = ["../outside"]
        self.assertIsNone(validated_saved_ingestion(state))

    def test_windows_selection_matches_case_without_changing_global_os(self):
        with mock.patch.object(file_selection, "os", SimpleNamespace(name="nt")):
            self.assertTrue(file_selection.path_selected("DOCS/Guide.TXT", ["docs"], []))
            self.assertTrue(file_selection.path_selected("DOCS", ["docs/nested"], [], directory=True))
            self.assertFalse(file_selection.path_selected("DOCS/ARCHIVE/Old.TXT", ["docs"], ["docs/archive"]))
            self.assertFalse(file_selection.path_selected("Docs/DRAFT.TXT", ["docs"], ["**/draft.*"]))
            self.assertFalse(file_selection.path_selected("OTHER/Guide.TXT", ["docs"], []))


if __name__ == "__main__":
    unittest.main()
