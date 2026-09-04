from __future__ import annotations

import argparse
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest import mock

from software_rag_tool import incremental
from software_rag_tool.embeddings import DocumentTokenBudget


REBUILD_PATH = Path(__file__).resolve().parents[2] / "rebuild_component.py"
_SPEC = importlib.util.spec_from_file_location("scope_authority_rebuild", REBUILD_PATH)
assert _SPEC is not None and _SPEC.loader is not None
rebuild = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rebuild)


class _Tokenizer:
    def __call__(self, text, **_kwargs):
        return {"input_ids": list(range(len(text) + 2))}


TOKEN_BUDGET = DocumentTokenBudget(
    tokenizer=_Tokenizer(),
    document_prefix="document: ",
    tokenizer_name="scope-authority-test-double",
    target_tokens=320,
    max_tokens=384,
)


class ExtractRebuildScopeAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.logs = self.directory / "logs"
        self.logs.mkdir()
        self.root = self.directory / "source"
        self.root.mkdir()
        self.patch = mock.patch.object(rebuild, "logs_dir", return_value=self.logs)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def _write(self, name: str, value: object) -> None:
        (self.logs / name).write_text(json.dumps(value), encoding="utf-8")

    def _scope(self, **overrides) -> dict:
        return {
            "root": str(self.root),
            "source_id": "canonical-source",
            "scan_subdir": "docs",
            "include_root_name_in_path": True,
            "operation": "add",
            "chunk_max_chars": 900,
            "chunk_overlap": 0,
            **overrides,
        }

    def test_canonical_scope_ignores_missing_corrupt_or_stale_progress(self) -> None:
        scope = self._scope(resolved_root="sha256:stable-source-identity")
        self._write("index_state.json", {"version": 2, "ingestion": scope})
        for progress in (None, "invalid-json", json.dumps(self._scope(source_id="stale"))):
            with self.subTest(progress=progress is None):
                path = self.logs / "progress.json"
                if progress is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(progress, encoding="utf-8")
                with (
                    mock.patch.object(rebuild, "add_or_update_root", return_value={}) as add,
                    redirect_stdout(io.StringIO()),
                ):
                    rebuild._rebuild(
                        argparse.Namespace(component="extract", batch_size_files=7),
                        "fixture-rag",
                    )
                add.assert_called_once_with(
                    root=self.root,
                    source_id="canonical-source",
                    scan_subdir="docs",
                    include_root_name_in_path=True,
                    batch_size_files=7,
                    reset_db=True,
                    reset_clean=True,
                    retry_errors=True,
                    operation="add",
                    chunk_max_chars=900,
                    chunk_overlap=0,
                    persistent_root_identity="sha256:stable-source-identity",
                )

    def test_old_canonical_scope_uses_defaults_not_progress_options(self) -> None:
        self._write("index_state.json", {"ingestion": {
            "root": str(self.root), "source_id": "canonical-source",
        }})
        self._write("progress.json", self._scope())
        scope = rebuild._extract_rebuild_scope()
        self.assertEqual(".", scope["scan_subdir"])
        self.assertEqual("build", scope["operation"])
        self.assertEqual(1400, scope["chunk_max_chars"])
        self.assertEqual(160, scope["chunk_overlap"])

    def test_legacy_progress_used_only_without_canonical_scope_key(self) -> None:
        self._write("progress.json", self._scope())
        self.assertEqual("canonical-source", rebuild._extract_rebuild_scope()["source_id"])
        self._write("index_state.json", {"version": 1, "files": {}})
        self.assertEqual(0, rebuild._extract_rebuild_scope()["chunk_overlap"])

    def test_malformed_or_incomplete_canonical_scope_never_falls_back(self) -> None:
        self._write("progress.json", self._scope(source_id="stale"))
        malformed = [
            None, [], {}, {"root": str(self.root)},
            self._scope(root="relative-source"),
            self._scope(source_id=17),
            self._scope(scan_subdir=None),
            self._scope(operation=""),
            self._scope(chunk_max_chars=True),
            self._scope(chunk_overlap="0"),
            self._scope(chunk_overlap=900),
            self._scope(include_root_name_in_path=False),
            self._scope(privacy_safe_root="false"),
            self._scope(resolved_root=None),
        ]
        for value in malformed:
            with self.subTest(case=malformed.index(value)):
                self._write("index_state.json", {"ingestion": value})
                with (
                    mock.patch.object(rebuild, "add_or_update_root") as add,
                    self.assertRaises(RuntimeError),
                ):
                    rebuild._rebuild(
                        argparse.Namespace(component="extract", batch_size_files=7),
                        "fixture-rag",
                    )
                add.assert_not_called()

    def test_corrupt_or_nonobject_state_never_falls_back(self) -> None:
        self._write("progress.json", self._scope())
        for value in ("invalid-json", "[]", "null"):
            with self.subTest(value=value):
                (self.logs / "index_state.json").write_text(value, encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    rebuild._extract_rebuild_scope()

    def test_private_authority_rejects_without_reading_legacy_plaintext_root(self) -> None:
        self._write("progress.json", self._scope())
        for private in (True, False):
            with self.subTest(private=private):
                self._write("index_state.json", {"ingestion": self._scope(
                    root="<EXTERNAL_SOURCE_ROOT>",
                    resolved_root="sha256:private-source-identity",
                    privacy_safe_root=private,
                )})
                with self.assertRaisesRegex(RuntimeError, "original root") as failure:
                    rebuild._extract_rebuild_scope()
                self.assertNotIn(str(self.root), str(failure.exception))


class DurableIngestionScopeTests(unittest.TestCase):
    def _prepare(self, stack: ExitStack, root: Path, logs: Path) -> None:
        root.mkdir()
        logs.mkdir()
        stack.enter_context(mock.patch.object(incremental, "logs_dir", return_value=logs))
        stack.enter_context(mock.patch.object(incremental, "require_index_tokenizer"))
        stack.enter_context(mock.patch.object(incremental, "validate_existing_index_tokenizer"))
        stack.enter_context(mock.patch.object(incremental, "emit_event"))

    def test_scope_options_are_durable_before_first_progress_and_discovery_failure(self) -> None:
        for private in (False, True):
            with self.subTest(private=private), tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
                root = Path(temporary) / "source"
                logs = Path(temporary) / "logs"
                self._prepare(stack, root, logs)
                observed = []

                def progress(**_updates):
                    observed.append(json.loads((logs / "index_state.json").read_text(encoding="utf-8")))

                stack.enter_context(mock.patch.object(incremental, "write_progress", side_effect=progress))
                stack.enter_context(mock.patch.object(incremental, "iter_input_files", side_effect=RuntimeError("fixture scan failed")))
                with self.assertRaises(RuntimeError):
                    incremental.add_or_update_root(
                        root, "source-fixture", operation="add", chunk_max_chars=900,
                        chunk_overlap=0, privacy_safe_root=private,
                        document_token_budget=TOKEN_BUDGET,
                    )
                self.assertTrue(observed)
                scope = observed[0]["ingestion"]
                self.assertEqual("source-fixture", scope["source_id"])
                self.assertEqual("add", scope["operation"])
                self.assertEqual(900, scope["chunk_max_chars"])
                self.assertEqual(0, scope["chunk_overlap"])
                self.assertEqual(".", scope["scan_subdir"])
                if private:
                    self.assertEqual("<EXTERNAL_SOURCE_ROOT>", scope["root"])
                    self.assertTrue(scope["resolved_root"].startswith("sha256:"))
                    self.assertNotIn(str(root), (logs / "index_state.json").read_text(encoding="utf-8"))
                else:
                    self.assertEqual(str(root), scope["root"])

    def test_scope_save_failure_remains_fatal_before_progress_discovery_or_vectors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary) / "source"
            logs = Path(temporary) / "logs"
            self._prepare(stack, root, logs)
            stack.enter_context(mock.patch.object(incremental, "_save_state", side_effect=PermissionError("state locked")))
            progress = stack.enter_context(mock.patch.object(incremental, "write_progress"))
            discover = stack.enter_context(mock.patch.object(incremental, "iter_input_files"))
            delete = stack.enter_context(mock.patch.object(incremental, "delete_ids"))
            upsert = stack.enter_context(mock.patch.object(incremental, "upsert_records"))
            with self.assertRaisesRegex(PermissionError, "state locked"):
                incremental.add_or_update_root(root, "source-fixture", document_token_budget=TOKEN_BUDGET)
            for action in (progress, discover, delete, upsert):
                action.assert_not_called()

    def test_private_same_scope_resume_retains_identity_and_batch_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary) / "source"
            logs = Path(temporary) / "logs"
            self._prepare(stack, root, logs)
            stack.enter_context(mock.patch.object(incremental, "write_progress"))
            stack.enter_context(mock.patch.object(incremental, "iter_input_files", side_effect=RuntimeError("fixture scan failed")))
            for resume in (False, True):
                with self.assertRaisesRegex(RuntimeError, "RuntimeError stage=run"):
                    incremental.add_or_update_root(
                        root, "source-fixture", batch_size_files=None if resume else 3,
                        resume=resume, privacy_safe_root=True, chunk_max_chars=900,
                        chunk_overlap=0, document_token_budget=TOKEN_BUDGET,
                    )
                saved = json.loads((logs / "index_state.json").read_text(encoding="utf-8"))["ingestion"]
                self.assertEqual(3, saved["batch_size_files"])
                self.assertEqual(0, saved["chunk_overlap"])
                self.assertTrue(saved["privacy_safe_root"])
                self.assertTrue(saved["resolved_root"].startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
