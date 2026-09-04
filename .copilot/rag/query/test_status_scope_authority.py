from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


RAG_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = RAG_ROOT / "gen_db/software_rag_tool"
for directory in (RAG_ROOT, TOOL_ROOT):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

SPEC = importlib.util.spec_from_file_location(
    "status_scope_authority_under_test", RAG_ROOT / "gen_db/status.py"
)
assert SPEC is not None and SPEC.loader is not None
status = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(status)

MISSING = object()
MALFORMED_JSON = object()


class StatusScopeAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="status-scope-authority-")
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Path(self.temporary.name)
        self.dbs_root = self.fixture / "dbs"
        self.database = self.dbs_root / "fixture-rag"
        (self.database / "logs").mkdir(parents=True)
        (self.database / "index").mkdir()
        self.canonical = {
            "root": str(self.fixture / "canonical-input"),
            "source_id": "canonical-source",
            "scan_subdir": "docs/current",
            "operation": "build",
            "batch_size_files": 7,
            "chunk_max_chars": 1400,
            "chunk_overlap": 160,
        }
        self.saved_files = {
            "one": {"status": "indexed", "record_ids": ["one", "two"]},
            "two": {"status": "indexed", "record_ids": ["three", "four"]},
            "error": {"status": "error", "record_ids": []},
        }
        (self.database / "index/manifest.json").write_text(
            json.dumps({"record_count": 4}), encoding="utf-8"
        )

    def progress(self, scope=None):
        return {
            **(self.canonical if scope is None else scope),
            "status": "failed",
            "phase": "embedding",
            "updated_at": "2001-01-01T00:00:00Z",
            "files_total": 91,
            "files_done": 92,
            "indexed_files": 93,
            "skipped_files": 94,
            "error_files": 95,
            "upserted_records": 96,
            "deleted_records": 97,
            "collection_count": 98,
            "current_file": "previous-only.txt",
            "current_batch_files": ["previous-only.txt"],
            "current_batch_records_done": 99,
            "current_batch_records_total": 100,
            "last_error": "previous-scope-error",
        }

    @staticmethod
    def _write_fixture(path, value):
        if value is MISSING:
            path.unlink(missing_ok=True)
        elif value is MALFORMED_JSON:
            path.write_text('{"unfinished":', encoding="utf-8")
        else:
            path.write_text(json.dumps(value), encoding="utf-8")

    def read_status(self, *, scope=MISSING, snapshot=MISSING, state_document=MISSING):
        canonical = self.canonical if scope is MISSING else scope
        state = (
            {"ingestion": canonical, "files": self.saved_files}
            if state_document is MISSING
            else state_document
        )
        self._write_fixture(self.database / "logs/index_state.json", state)
        self._write_fixture(self.database / "logs/progress.json", snapshot)
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"RAG_DBS_ROOT": str(self.dbs_root)}),
            mock.patch.object(sys, "argv", ["status.py", "--db", "fixture-rag", "--json"]),
            mock.patch.object(status, "load_env"),
            mock.patch.object(status, "read_db_version", return_value={}),
            mock.patch.object(status, "catalog_counts", return_value={"exists": True, "chunks": 4, "documents": 2}),
            redirect_stdout(output),
        ):
            status.main()
        return json.loads(output.getvalue())

    def assert_canonical_commands(self, result, scope=None):
        canonical = self.canonical if scope is None else scope
        self.assertTrue(result["scope_valid"])
        self.assertEqual(canonical, result["ingestion"])
        for field in ("root", "source_id", "scan_subdir", "operation", "batch_size_files"):
            self.assertEqual(canonical[field], result[field], field)
        self.assertTrue(result["can_resume"])
        command = result["resume_command"]
        self.assertEqual(
            "build_db.py" if canonical["operation"] == "build" else "add_data.py",
            Path(command[1]).name,
        )
        for flag, field in (("--root", "root"), ("--source-id", "source_id"), ("--batch-size-files", "batch_size_files")):
            self.assertEqual(str(canonical[field]), command[command.index(flag) + 1])
        self.assertIn("--resume", command)
        if canonical["scan_subdir"] != ".":
            self.assertEqual(canonical["scan_subdir"], command[command.index("--scan-subdir") + 1])
        force = result["force_rebuild_command"]
        self.assertTrue(force)
        self.assertEqual(canonical["root"], force[force.index("--root") + 1])
        self.assertEqual(canonical["source_id"], force[force.index("--source-id") + 1])

    def assert_progress_unavailable(self, result):
        self.assertFalse(result["progress_matches_scope"])
        self.assertEqual("progress_unavailable", result["status"])
        self.assertFalse(result["appears_active"])
        self.assertEqual("", result["current_file"])
        self.assertEqual([], result["current_batch_files"])
        self.assertEqual("", result["last_error"])
        for field in ("files_total", "files_done", "skipped_files", "upserted_records", "deleted_records", "current_batch_records_done", "current_batch_records_total"):
            self.assertEqual(0, result[field], field)
        self.assertEqual(2, result["indexed_files"])
        self.assertEqual(1, result["error_files"])
        self.assertEqual(4, result["collection_count"])

    def test_stale_progress_cannot_override_canonical_scope_or_run_counts(self):
        stale = {
            "root": str(self.fixture / "previous-input"),
            "source_id": "previous-source",
            "scan_subdir": "previous-docs",
            "operation": "add",
            "batch_size_files": 1,
        }
        result = self.read_status(snapshot=self.progress(stale))
        # First assertion reproduces the original progress-first control bug.
        self.assertEqual(self.canonical["root"], result["root"])
        self.assert_canonical_commands(result)
        self.assert_progress_unavailable(result)

    def test_same_scope_with_different_operation_does_not_reuse_progress(self):
        snapshot = self.progress()
        snapshot["operation"] = "add"
        result = self.read_status(snapshot=snapshot)
        self.assert_canonical_commands(result)
        self.assert_progress_unavailable(result)

    def test_batch_only_mismatch_does_not_reuse_progress(self):
        snapshot = self.progress()
        snapshot["batch_size_files"] = 1
        result = self.read_status(snapshot=snapshot)
        self.assert_canonical_commands(result)
        self.assert_progress_unavailable(result)

    def test_progress_operation_is_required_even_when_other_fields_match(self):
        snapshot = self.progress()
        del snapshot["operation"]
        result = self.read_status(snapshot=snapshot)
        self.assert_canonical_commands(result)
        self.assert_progress_unavailable(result)

    def test_progress_batch_comparison_is_type_strict(self):
        canonical = {**self.canonical, "batch_size_files": 1}
        for value in (True, 1.0, "1"):
            with self.subTest(value=value):
                snapshot = self.progress(canonical)
                snapshot["batch_size_files"] = value
                result = self.read_status(scope=canonical, snapshot=snapshot)
                self.assert_canonical_commands(result, canonical)
                self.assert_progress_unavailable(result)

    def test_all_optional_scope_control_fields_must_match(self):
        canonical = {
            **self.canonical,
            "resolved_root": str(self.fixture / "canonical-input"),
            "root_display_name": "canonical-input",
            "scan_root": str(self.fixture / "canonical-input/docs/current"),
            "stored_path_prefix": "canonical-input/docs/current",
            "include_root_name_in_path": True,
            "privacy_safe_root": False,
        }
        changes = {
            "resolved_root": "previous-resolved",
            "root_display_name": "previous-name",
            "scan_root": "previous-scan-root",
            "stored_path_prefix": "previous-prefix",
            "include_root_name_in_path": False,
            "privacy_safe_root": True,
        }
        for field, value in changes.items():
            with self.subTest(field=field, relation="different"):
                snapshot = self.progress(canonical)
                snapshot[field] = value
                result = self.read_status(scope=canonical, snapshot=snapshot)
                self.assert_canonical_commands(result, canonical)
                self.assert_progress_unavailable(result)
            with self.subTest(field=field, relation="missing-from-progress"):
                snapshot = self.progress(canonical)
                del snapshot[field]
                result = self.read_status(scope=canonical, snapshot=snapshot)
                self.assert_canonical_commands(result, canonical)
                self.assert_progress_unavailable(result)
            with self.subTest(field=field, relation="only-in-progress"):
                snapshot = self.progress()
                snapshot[field] = canonical[field]
                result = self.read_status(snapshot=snapshot)
                self.assert_canonical_commands(result)
                self.assert_progress_unavailable(result)

    def test_matching_progress_retains_normal_status_counts_and_current_file(self):
        snapshot = self.progress()
        result = self.read_status(snapshot=snapshot)
        self.assert_canonical_commands(result)
        self.assertTrue(result["progress_matches_scope"])
        for field in ("status", "phase", "updated_at", "files_total", "files_done", "indexed_files", "skipped_files", "error_files", "upserted_records", "deleted_records", "collection_count", "current_file", "current_batch_files", "current_batch_records_done", "current_batch_records_total", "last_error"):
            self.assertEqual(snapshot[field], result[field], field)

    def test_current_and_legacy_terminal_statuses_allow_canonical_resume(self):
        for terminal in ("success", "partial", "failure", "completed", "failed"):
            with self.subTest(terminal=terminal):
                snapshot = {**self.progress(), "status": terminal}
                result = self.read_status(snapshot=snapshot)
                self.assert_canonical_commands(result)
                self.assertTrue(result["progress_matches_scope"])
                self.assertEqual(terminal, result["status"])

    def test_current_running_progress_does_not_offer_resume(self):
        snapshot = {**self.progress(), "status": "running", "updated_at": "2999-01-01T00:00:00Z"}
        result = self.read_status(snapshot=snapshot)
        self.assertTrue(result["progress_matches_scope"])
        self.assertTrue(result["appears_active"])
        self.assertFalse(result["can_resume"])

    def test_missing_or_invalid_canonical_state_never_builds_progress_commands(self):
        documents = (
            {}, [], None, {"files": self.saved_files}, {"ingestion": []},
            {"ingestion": None},
        )
        for index, state_document in enumerate(documents):
            with self.subTest(case=index):
                result = self.read_status(snapshot=self.progress(), state_document=state_document)
                self.assertFalse(result["scope_valid"])
                self.assertFalse(result["progress_matches_scope"])
                self.assertEqual({}, result["ingestion"])
                self.assertEqual([], result["resume_command"])
                self.assertEqual([], result["force_rebuild_command"])
                self.assertFalse(result["can_resume"])
                self.assertNotEqual(self.canonical["root"], result["root"])
        # A broken authority remains a hard read failure, never best-effort
        # success nor a fallback to a parseable but stale progress document.
        with self.assertRaises(json.JSONDecodeError):
            self.read_status(snapshot=self.progress(), state_document=MALFORMED_JSON)

    def test_missing_canonical_file_never_builds_progress_commands(self):
        self._write_fixture(self.database / "logs/progress.json", self.progress())
        # MISSING is also read_status's default sentinel, so patch only fixture
        # publication here to explicitly leave index_state.json absent.
        original_write = self._write_fixture

        def write(path, value):
            return original_write(path, MISSING if path.name == "index_state.json" else value)

        with mock.patch.object(self, "_write_fixture", side_effect=write):
            result = self.read_status(snapshot=self.progress())
        self.assertFalse(result["scope_valid"])
        self.assertFalse(result["can_resume"])
        self.assertEqual([], result["resume_command"])
        self.assertEqual([], result["force_rebuild_command"])

    def test_invalid_required_canonical_fields_do_not_fall_back_to_progress(self):
        invalid_values = {
            "root": (None, "", "   ", 3),
            "source_id": (None, "", "   ", 3),
            "scan_subdir": (None, "", 3),
            "operation": (None, "", "rebuild", 3),
            "batch_size_files": (None, 0, -1, True, 1.0, "7"),
        }
        for field, values in invalid_values.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    invalid = {**self.canonical, field: value}
                    result = self.read_status(scope=invalid, snapshot=self.progress())
                    self.assertFalse(result["scope_valid"])
                    self.assertFalse(result["can_resume"])
                    self.assertEqual([], result["resume_command"])
                    self.assertEqual([], result["force_rebuild_command"])

    def test_missing_required_canonical_fields_do_not_fall_back_to_progress(self):
        for field in ("root", "source_id", "scan_subdir", "operation", "batch_size_files"):
            with self.subTest(field=field):
                invalid = dict(self.canonical)
                del invalid[field]
                result = self.read_status(scope=invalid, snapshot=self.progress())
                self.assertFalse(result["scope_valid"])
                self.assertFalse(result["can_resume"])
                self.assertEqual([], result["resume_command"])
                self.assertEqual([], result["force_rebuild_command"])

    def test_missing_or_invalid_progress_still_uses_canonical_commands(self):
        for index, snapshot in enumerate((MISSING, MALFORMED_JSON, [], None, "invalid", {})):
            with self.subTest(case=index):
                result = self.read_status(snapshot=snapshot)
                self.assert_canonical_commands(result)
                self.assert_progress_unavailable(result)

    def test_add_operation_and_dot_scope_work_without_optional_fields(self):
        canonical = {**self.canonical, "operation": "add", "scan_subdir": "."}
        result = self.read_status(scope=canonical)
        self.assert_canonical_commands(result, canonical)
        self.assertNotIn("--scan-subdir", result["resume_command"])

    def test_private_canonical_scope_never_generates_root_commands(self):
        canonical = {
            **self.canonical,
            "root": "<EXTERNAL_SOURCE_ROOT>",
            "resolved_root": "sha256:" + "a" * 64,
            "privacy_safe_root": True,
        }
        result = self.read_status(scope=canonical, snapshot=self.progress(canonical))
        self.assertTrue(result["scope_valid"])
        self.assertEqual(canonical, result["ingestion"])
        self.assertFalse(result["can_resume"])
        self.assertEqual([], result["resume_command"])
        self.assertEqual([], result["force_rebuild_command"])

    def test_private_plaintext_shape_is_rejected_before_scope_display(self):
        canonical = {
            **self.canonical, "root": "<EXTERNAL_SOURCE_ROOT>",
            "resolved_root": "sha256:" + "a" * 64, "privacy_safe_root": True,
        }
        for field in ("root", "resolved_root", "scan_root", "root_display_name", "stored_path_prefix"):
            with self.subTest(field=field):
                scope = {**canonical, field: self.canonical["root"]}
                result = self.read_status(scope=scope, snapshot=self.progress(scope))
                self.assertFalse(result["scope_valid"])
                self.assertEqual({}, result["ingestion"])
                self.assertFalse(result["can_resume"])
                self.assertEqual([], result["resume_command"])
                self.assertEqual([], result["force_rebuild_command"])
                self.assertNotIn(self.canonical["root"], json.dumps(result, ensure_ascii=False).replace("\\\\", "\\"))

    def test_scope_output_does_not_echo_unknown_legacy_fields(self):
        canonical = {
            **self.canonical, "root": "<EXTERNAL_SOURCE_ROOT>",
            "resolved_root": "sha256:" + "a" * 64, "privacy_safe_root": True,
        }
        stored = {**canonical, "legacy_root": self.canonical["root"]}
        result = self.read_status(scope=stored, snapshot=self.progress(canonical))
        self.assertTrue(result["scope_valid"])
        self.assertEqual(canonical, result["ingestion"])
        self.assertNotIn("legacy_root", result["ingestion"])
        self.assertNotIn(self.canonical["root"], json.dumps(result, ensure_ascii=False).replace("\\\\", "\\"))


if __name__ == "__main__":
    unittest.main()
