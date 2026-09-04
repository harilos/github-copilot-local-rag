from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from software_rag_tool import incremental
from software_rag_tool.ingestion_paths import resolve_ingestion_scope
from test_rebuild_scope_authority import TOKEN_BUDGET


class ResumeRootIdentityTests(unittest.TestCase):
    """Exercise real checkpoint/resume flow with isolated, mocked store writes."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "filtered" / "source"
        self.original = self.base / "original" / "source"
        self.other = self.base / "other" / "source"
        self.output = self.base / "db"
        for root in (self.root, self.original, self.other):
            (root / "docs").mkdir(parents=True)
            (root / "other-docs").mkdir()
            (root / "docs" / "fixture.txt").write_text("fixture", encoding="utf-8")
        self.state_path = self.output / "logs" / "index_state.json"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, {"RAG_OUTPUT_ROOT": str(self.output)}))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        for name in ("require_index_tokenizer", "validate_existing_index_tokenizer", "write_manifest"):
            self.stack.enter_context(mock.patch.object(incremental, name))
        self.progress = self.stack.enter_context(mock.patch.object(incremental, "write_progress"))
        self.stack.enter_context(mock.patch.object(incremental, "emit_event"))
        self.stack.enter_context(mock.patch.object(incremental, "update_profile_from_clean", return_value=False))
        self.stack.enter_context(mock.patch.object(incremental, "collection_count", return_value=1))
        self.records = self.stack.enter_context(mock.patch.object(
            incremental, "build_records_for_file",
            return_value=[{"id": "fixture-record", "text": "fixture"}],
        ))
        self.upsert = self.stack.enter_context(mock.patch.object(
            incremental, "upsert_records", side_effect=lambda values, **_kwargs: len(values),
        ))
        self.delete = self.stack.enter_context(mock.patch.object(incremental, "delete_ids", return_value=0))
        self.catalog = self.stack.enter_context(mock.patch.object(incremental, "upsert_catalog_records"))
        self.stack.enter_context(mock.patch.object(incremental, "delete_catalog_chunks", return_value=0))

    def run_add(self, **overrides):
        arguments = {
            "root": self.root,
            "source_id": "fixture-source",
            "scan_subdir": "docs",
            "batch_size_files": 3,
            "chunk_max_chars": 900,
            "chunk_overlap": 0,
            "document_token_budget": TOKEN_BUDGET,
            **overrides,
        }
        return incremental.add_or_update_root(**arguments)

    def saved_scope(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))["ingestion"]

    def assert_resume_rejected(self, expected_field, **arguments):
        before = self.state_path.read_bytes()
        for action in (self.progress, self.upsert, self.delete, self.catalog, self.records):
            action.reset_mock()
        with self.assertRaisesRegex(ValueError, expected_field):
            self.run_add(resume=True, **arguments)
        self.assertEqual(before, self.state_path.read_bytes())
        for action in (self.progress, self.upsert, self.delete, self.catalog, self.records):
            action.assert_not_called()

    def test_filtered_scope_resumes_with_saved_original_identity(self):
        identity = str(self.original.resolve())
        first = self.run_add(persistent_root_identity=identity)
        saved = self.saved_scope()
        self.assertEqual("success", first["result_status"])
        self.assertNotEqual(saved["resolved_root"], str(self.root.resolve()))
        self.records.reset_mock()
        resumed = self.run_add(
            root=Path(saved["root"]), source_id=saved["source_id"],
            scan_subdir=saved["scan_subdir"], batch_size_files=None,
            persistent_root_identity=saved["resolved_root"], resume=True,
        )
        self.assertEqual("success", resumed["result_status"])
        self.assertEqual(1, resumed["skipped_files"])
        self.assertEqual(3, self.saved_scope()["batch_size_files"])
        self.assertEqual(identity, self.saved_scope()["resolved_root"])
        self.records.assert_not_called()

    def test_saved_identity_does_not_authorize_other_root_source_scope_or_batch(self):
        identity = str(self.original.resolve())
        self.run_add(persistent_root_identity=identity)
        changes = (
            ("root", {"root": self.other}),
            ("source_id", {"source_id": "other-source"}),
            ("scan_subdir", {"scan_subdir": "other-docs"}),
            ("batch_size_files", {"batch_size_files": 4}),
            ("resolved_root", {"persistent_root_identity": "sha256:unrelated"}),
        )
        for field, values in changes:
            with self.subTest(field=field):
                self.assert_resume_rejected(
                    field, **{"persistent_root_identity": identity, **values},
                )

    def test_same_logical_root_cannot_mask_a_changed_resolved_scan_root(self):
        identity = str(self.original.resolve())
        self.run_add(persistent_root_identity=identity)
        changed = replace(
            resolve_ingestion_scope(self.root, "docs"),
            resolved_root=self.other.resolve(),
            scan_root=(self.other / "docs").resolve(),
        )
        with mock.patch.object(incremental, "resolve_ingestion_scope", return_value=changed):
            self.assert_resume_rejected("scan_root", persistent_root_identity=identity)

    def test_custom_identity_without_saved_physical_scope_evidence_is_rejected(self):
        identity = str(self.original.resolve())
        self.run_add(persistent_root_identity=identity)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["ingestion"].pop("scan_root")
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        self.assert_resume_rejected("scan_root", persistent_root_identity=identity)

    def test_missing_custom_identity_is_not_inferred_from_saved_state(self):
        self.run_add(persistent_root_identity=str(self.original.resolve()))
        self.assert_resume_rejected("resolved_root")

    def test_private_same_root_normal_hash_resumes_with_or_without_explicit_identity(self):
        self.run_add(privacy_safe_root=True)
        identity = self.saved_scope()["resolved_root"]
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                resumed = self.run_add(
                    resume=True, privacy_safe_root=True,
                    persistent_root_identity=identity if explicit else None,
                )
                self.assertEqual("success", resumed["result_status"])
                self.assertEqual(1, resumed["skipped_files"])
                self.assertEqual(identity, self.saved_scope()["resolved_root"])
                self.assertEqual("<EXTERNAL_SOURCE_ROOT>", self.saved_scope()["root"])
                self.assertNotIn(str(self.root), self.state_path.read_text(encoding="utf-8"))

    def test_private_resume_validates_before_scope_migration(self):
        self.run_add(privacy_safe_root=True)
        identity = self.saved_scope()["resolved_root"]
        changes = (
            ("resolved_root", {"root": self.other}),
            ("resolved_root", {"root": self.other, "persistent_root_identity": identity}),
            ("source_id", {"source_id": "other-source"}),
            ("scan_subdir", {"scan_subdir": "other-docs"}),
            ("batch_size_files", {"batch_size_files": 4}),
        )
        for field, values in changes:
            with self.subTest(field=field, explicit="persistent_root_identity" in values):
                self.assert_resume_rejected(field, privacy_safe_root=True, **values)

    def test_private_custom_identity_without_physical_proof_stops_safely(self):
        identity = "sha256:unverifiable-original-root"
        self.run_add(privacy_safe_root=True, persistent_root_identity=identity)
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                self.assert_resume_rejected(
                    "resolved_root", privacy_safe_root=True,
                    persistent_root_identity=identity if explicit else None,
                )


if __name__ == "__main__":
    unittest.main()
