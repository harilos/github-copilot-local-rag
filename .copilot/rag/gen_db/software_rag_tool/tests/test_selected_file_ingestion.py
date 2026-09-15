from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import ExitStack, nullcontext, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from software_rag_tool import incremental
from software_rag_tool.ingestion_paths import validated_saved_ingestion
from test_rebuild_scope_authority import TOKEN_BUDGET


class SelectedFileIngestionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "gitlab-source"
        self.root.mkdir()
        self.output = self.base / "db"
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.dict(os.environ, {"RAG_OUTPUT_ROOT": str(self.output)}))
        stack.enter_context(redirect_stdout(io.StringIO()))
        for name in ("require_index_tokenizer", "validate_existing_index_tokenizer",
                     "write_manifest", "write_progress", "emit_event"):
            stack.enter_context(mock.patch.object(incremental, name))
        self.catalog = stack.enter_context(mock.patch.object(incremental, "upsert_catalog_records"))
        stack.enter_context(mock.patch.object(incremental, "collection_count", return_value=1))
        stack.enter_context(mock.patch.object(incremental, "update_profile_from_clean", return_value=False))
        stack.enter_context(mock.patch.object(incremental, "upsert_records", side_effect=lambda values, **_kw: len(values)))
        self.delete = stack.enter_context(mock.patch.object(incremental, "delete_ids", side_effect=len))
        self.catalog_delete = stack.enter_context(mock.patch.object(incremental, "delete_catalog_chunks", side_effect=len))
        self.hash = stack.enter_context(mock.patch.object(incremental, "file_content_hash", wraps=incremental.file_content_hash))
        self.records = stack.enter_context(mock.patch.object(incremental, "build_records_for_file", wraps=incremental.build_records_for_file))

    def write(self, relative, text="fixture document"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run_add(self, **overrides):
        return incremental.add_or_update_root(**{
            "root": self.root, "source_id": "gitlab-issues", "batch_size_files": 20,
            "document_token_budget": TOKEN_BUDGET, **overrides,
        })

    def state(self):
        return json.loads((self.output / "logs" / "index_state.json").read_text(encoding="utf-8"))

    def entries(self):
        return {entry["path"]: entry for entry in self.state()["files"].values()}

    def test_two_selected_files_do_not_scan_or_hash_hundred_other_documents(self):
        for number in range(102):
            self.write(f"issues/{number}.md", f"issue {number} before")
        self.run_add()
        before = self.entries()
        preserved = before["gitlab-source/issues/101.md"]
        preserved_clean = self.output / "data" / "clean" / preserved["records_path"]
        preserved_body = preserved_clean.read_bytes()
        self.write("issues/0.md", "updated issue zero")
        self.write("issues/1.md", "updated issue one")
        (self.root / "issues/101.md").unlink()
        self.hash.reset_mock()
        self.records.reset_mock()
        self.delete.reset_mock()
        self.catalog_delete.reset_mock()
        self.catalog.reset_mock()
        with (
            mock.patch.object(incremental, "iter_input_files", side_effect=AssertionError("unexpected tree scan")),
            mock.patch.object(incremental, "_unsupported_input_paths", side_effect=AssertionError("unexpected diagnostic scan")),
            mock.patch.object(incremental, "_reconcile_missing_files", side_effect=AssertionError("unexpected deletion reconciliation")),
        ):
            result = self.run_add(selected_files=["issues/1.md", "issues/0.md", "issues/1.md"])
        self.assertEqual(2, self.hash.call_count)
        self.assertEqual(2, self.records.call_count)
        self.assertEqual({"0.md", "1.md"}, {call.args[0].name for call in self.hash.call_args_list})
        self.assertEqual((2, 2, 0, 102), (result["file_count"], result["indexed_files"], result["deleted_files"], result["searchable_files"]))
        after = self.entries()
        for path, entry in before.items():
            if path not in {"gitlab-source/issues/0.md", "gitlab-source/issues/1.md"}:
                self.assertEqual(entry, after[path])
        expected_deleted = {
            record_id for path in ("gitlab-source/issues/0.md", "gitlab-source/issues/1.md")
            for record_id in before[path]["record_ids"]
        }
        self.assertEqual(expected_deleted, {value for call in self.delete.call_args_list for value in call.args[0]})
        self.assertEqual(expected_deleted, {value for call in self.catalog.call_args_list for value in call.kwargs["delete_ids"]})
        self.catalog_delete.assert_not_called()
        self.assertEqual(preserved_body, preserved_clean.read_bytes())
        saved = validated_saved_ingestion(self.state())
        self.assertIsNotNone(saved)
        self.assertEqual(".", saved["scan_subdir"])
        self.assertEqual([], saved["include_paths"])
        self.assertNotIn("selected_files", self.state()["ingestion"])

    def test_empty_selection_is_upsert_only_but_none_retains_normal_deletion_semantics(self):
        self.write("issues/1.md")
        self.run_add()
        before = self.entries()
        (self.root / "issues/1.md").unlink()
        self.hash.reset_mock()
        result = self.run_add(selected_files=[])
        self.hash.assert_not_called()
        self.assertEqual(0, result["file_count"])
        self.assertEqual(0, result["deleted_files"])
        self.assertEqual(1, result["searchable_files"])
        self.assertEqual(before, self.entries())
        full = self.run_add()
        self.assertEqual(1, full["deleted_files"])
        self.assertEqual(0, full["searchable_files"])

    def test_earlier_failure_is_retried_in_later_batch_and_empty_retry(self):
        self.write("issues/1.md", "old one")
        self.write("issues/2.md", "old two")
        self.run_add()
        self.write("issues/1.md", "new one")
        original = self.records._mock_wraps

        def fail_first(root, path, *args, **kwargs):
            if path.name == "1.md":
                raise ValueError("fixture extraction failure")
            return original(root, path, *args, **kwargs)

        self.records.side_effect = fail_first
        first = self.run_add(selected_files=["issues/1.md"], retry_errors=True)
        self.assertEqual("partial", first["result_status"])
        self.write("issues/2.md", "new two")
        self.hash.reset_mock()
        second = self.run_add(selected_files=["issues/2.md"], retry_errors=True)
        self.assertEqual(("partial", 1, 1, 2), (second["result_status"], second["indexed_files"], second["error_files"], second["searchable_files"]))
        self.assertEqual({"1.md", "2.md"}, {call.args[0].name for call in self.hash.call_args_list})
        retry = self.run_add(selected_files=[], retry_errors=True)
        self.assertEqual(("partial", 1, 1), (retry["result_status"], retry["file_count"], retry["error_files"]))
        self.records.side_effect = None
        recovered = self.run_add(selected_files=[], retry_errors=True)
        self.assertEqual(("success", 1, 0, 2), (recovered["result_status"], recovered["indexed_files"], recovered["error_files"], recovered["searchable_files"]))
        self.assertEqual("indexed", self.entries()["gitlab-source/issues/1.md"]["status"])

    def test_missing_error_file_remains_retryable_without_deleting_old_records(self):
        path = self.write("issues/1.md")
        self.run_add()
        old_ids = self.entries()["gitlab-source/issues/1.md"]["record_ids"]
        path.unlink()
        first = self.run_add(selected_files=["issues/1.md"], retry_errors=True)
        second = self.run_add(selected_files=[], retry_errors=True)
        for result in (first, second):
            self.assertEqual(("partial", 1, 1, 0), (result["result_status"], result["input_error_files"], result["searchable_files"], result["deleted_files"]))
        entry = self.entries()["gitlab-source/issues/1.md"]
        self.assertEqual(old_ids, entry["record_ids"])
        self.assertTrue(entry["retryable"])

    def test_retry_only_includes_errors_for_same_source_and_root_identity(self):
        self.write("issues/1.md")
        self.records.side_effect = ValueError("fixture extraction failure")
        self.run_add(selected_files=["issues/1.md"], retry_errors=True, source_id="other-source")
        self.run_add(selected_files=["issues/1.md"], retry_errors=True, persistent_root_identity="other-root")
        self.records.side_effect = None
        self.hash.reset_mock()
        result = self.run_add(selected_files=[], retry_errors=True)
        self.assertEqual("success", result["result_status"])
        self.assertEqual(0, result["file_count"])
        self.hash.assert_not_called()
        self.assertEqual(2, len(self.state()["files"]))

    def test_invalid_paths_and_incompatible_options_rejected_before_state_writes(self):
        self.write("issues/1.md")
        (self.root / "directory.md").mkdir()
        cases = [
            {"selected_files": [path]} for path in (
                "../outside.md", "issues/../../outside.md", "/outside.md",
                "C:/outside.md", "C:outside.md", "issues\\1.md", "issues/one:stream.md", "", ".",
                "issues/binary.unsupported", "issues/\x00.md", "directory.md",
                ".git/readme.md", "issues/~$owner.docx",
            )
        ]
        cases.extend({"selected_files": [], **other} for other in (
            {"reset_db": True}, {"reset_clean": True}, {"resume": True},
            {"scan_subdir": "issues"}, {"include_paths": ["issues"]},
            {"exclude_paths": ["issues"]},
        ))
        for arguments in cases:
            with self.subTest(arguments=arguments), mock.patch.object(incremental, "_save_state") as save:
                with self.assertRaises(ValueError):
                    self.run_add(**arguments)
                save.assert_not_called()
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs unavailable")
    def test_fifo_rejected_before_hashing(self):
        os.mkfifo(self.root / "blocked.md")
        with self.assertRaisesRegex(ValueError, "regular files"):
            self.run_add(selected_files=["blocked.md"])
        self.hash.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_symlink_file_and_directory_escape_rejected_before_state_writes(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("private", encoding="utf-8")
        try:
            (self.root / "link").symlink_to(outside, target_is_directory=True)
            (self.root / "link.md").symlink_to(outside / "secret.md")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        for selected in (["link/secret.md"], ["link.md"]):
            with self.subTest(selected=selected), mock.patch.object(incremental, "_save_state") as save:
                with self.assertRaisesRegex(ValueError, "symbolic links"):
                    self.run_add(selected_files=selected)
                save.assert_not_called()

    def test_windows_reparse_attribute_rejected(self):
        path = self.write("reparse.md")
        real_lstat = Path.lstat

        def reparse_lstat(candidate, *args, **kwargs):
            if candidate == path:
                return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return real_lstat(candidate, *args, **kwargs)

        with mock.patch.object(Path, "lstat", reparse_lstat):
            with self.assertRaisesRegex(ValueError, "reparse points"):
                self.run_add(selected_files=["reparse.md"])
        self.hash.assert_not_called()

    def test_cli_distinguishes_none_empty_and_explicit_selection(self):
        path = Path(__file__).resolve().parents[2] / "add_data.py"
        spec = importlib.util.spec_from_file_location("selected_ingestion_add", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for options, expected in (
            ([], None), (["--selected-files-only"], []),
            (["--selected-file=issues/1.md"], ["issues/1.md"]),
            (["--selected-files-only", "--selected-file=issues/1.md", "--selected-file=issues/2.md"], ["issues/1.md", "issues/2.md"]),
        ):
            with (
                self.subTest(options=options),
                mock.patch.object(sys, "argv", ["add_data.py", "--db", "fixture-rag", "--root", str(self.root), *options]),
                mock.patch.object(module, "load_env"),
                mock.patch.object(module, "_preflight_estimated_documents", return_value=999) as preflight,
                mock.patch.object(module, "_AddProgressWatcher") as watcher,
                mock.patch.object(module, "database_writer_session", return_value=nullcontext(SimpleNamespace(db_root=self.output))),
                mock.patch.object(incremental, "add_or_update_root", return_value={}) as add,
            ):
                self.assertEqual(0, module.main())
                self.assertEqual(expected, add.call_args.kwargs["selected_files"])
                self.assertEqual(999 if expected is None else len(expected), watcher.call_args.kwargs["estimated_total"])
                if expected is not None:
                    preflight.assert_not_called()


if __name__ == "__main__":
    unittest.main()
