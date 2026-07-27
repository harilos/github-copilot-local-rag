from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


QUERY_ROOT = Path(__file__).resolve().parent
RAG_ROOT = QUERY_ROOT.parent
GEN_DB_ROOT = RAG_ROOT / "gen_db"
TOOL_ROOT = GEN_DB_ROOT / "software_rag_tool"
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool import incremental
from software_rag_tool.ingestion_paths import resolve_ingestion_scope
from software_rag_tool.records import build_records_for_file, iter_input_files


def _load_status_module():
    spec = importlib.util.spec_from_file_location(
        "rag_status_contract_module",
        GEN_DB_ROOT / "status.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load status.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STATUS = _load_status_module()


class IngestionScopePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.root = self.workspace / "プロジェクト資料"
        self.fy26 = self.root / "plans" / "FY26"
        self.fy27 = self.root / "plans" / "FY27"
        self.fy26.mkdir(parents=True)
        self.fy27.mkdir(parents=True)
        self.file26 = self.fy26 / "Design Report.md"
        self.file27 = self.fy27 / "Roadmap.md"
        self.file26.write_text(
            "# Design\nThe selected design is documented here.",
            encoding="utf-8",
        )
        self.file27.write_text("# Roadmap\nFuture work.", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_root_name_is_mandatory_and_japanese_spaces_are_preserved(self) -> None:
        scope = resolve_ingestion_scope(self.root)
        stored = scope.file(self.file26)
        self.assertEqual(
            "プロジェクト資料/plans/FY26/Design Report.md",
            stored.stored_path,
        )
        self.assertEqual("プロジェクト資料/", scope.stored_path_prefix)
        self.assertTrue(scope.include_root_name_in_path)
        self.assertNotIn("\\", stored.stored_path)
        self.assertFalse(Path(stored.stored_path).is_absolute())

    def test_scan_subdir_limits_discovery_but_not_stored_path_base(self) -> None:
        scope = resolve_ingestion_scope(self.root, "plans/FY26")
        discovered = list(iter_input_files(scope.scan_root))
        self.assertEqual(
            [self.file26.resolve()],
            [path.resolve() for path in discovered],
        )
        self.assertEqual(
            "プロジェクト資料/plans/FY26/Design Report.md",
            scope.file(discovered[0]).stored_path,
        )
        self.assertEqual("plans/FY26", scope.scan_subdir)

    def test_whole_root_dot_is_normalized(self) -> None:
        scope = resolve_ingestion_scope(self.root, ".")
        self.assertEqual(".", scope.scan_subdir)
        self.assertEqual(self.root.resolve(), scope.scan_root)

    def test_invalid_or_missing_scan_subdirectories_are_rejected(self) -> None:
        for value in (
            "/absolute/path",
            r"C:\absolute\path",
            "../outside",
            "foo/../../outside",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    resolve_ingestion_scope(self.root, value)
        with self.assertRaises(FileNotFoundError):
            resolve_ingestion_scope(self.root, "missing")

    def test_parent_and_child_scopes_create_the_same_identity(self) -> None:
        parent = resolve_ingestion_scope(self.root)
        child = resolve_ingestion_scope(self.root, "plans/FY26")
        parent_records = build_records_for_file(
            self.root,
            self.file26,
            source_id="project",
            ingestion_scope=parent,
        )
        child_records = build_records_for_file(
            self.root,
            self.file26,
            source_id="project",
            ingestion_scope=child,
        )
        self.assertEqual(
            parent_records[0]["doc_id"],
            child_records[0]["doc_id"],
        )
        self.assertEqual(
            parent_records[0]["metadata"]["path"],
            child_records[0]["metadata"]["path"],
        )

    def test_record_identity_and_metadata_use_one_canonical_stored_path(self) -> None:
        scope = resolve_ingestion_scope(self.root, "plans/FY26")
        record = build_records_for_file(
            self.root,
            self.file26,
            source_id="project",
            ingestion_scope=scope,
        )[0]
        stored_path = "プロジェクト資料/plans/FY26/Design Report.md"
        content_hash = record["metadata"]["content_hash"]
        expected_doc_id = hashlib.sha256(
            f"project:{stored_path}:{content_hash}".encode("utf-8")
        ).hexdigest()
        self.assertEqual(expected_doc_id, record["doc_id"])
        self.assertEqual(stored_path, record["metadata"]["path"])
        self.assertEqual(stored_path, record["metadata"]["uri"])
        self.assertEqual("プロジェクト資料", record["metadata"]["root"])
        self.assertTrue(record["embedding_text"].startswith(stored_path + "\n"))
        self.assertNotIn(str(self.workspace), str(record["metadata"]))

    def test_resume_rejects_a_different_scan_scope(self) -> None:
        saved_scope = resolve_ingestion_scope(self.root, "plans/FY26")
        requested_scope = resolve_ingestion_scope(self.root, "plans/FY27")
        state = {
            "version": 2,
            "files": {},
            "ingestion": saved_scope.state_fields(source_id="project"),
        }
        with self.assertRaisesRegex(ValueError, "scan_subdir"):
            incremental._validate_resume_state(
                state,
                requested_scope,
                "project",
            )

    def test_resume_cannot_reset_state_before_validation(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "resume cannot be combined",
        ):
            incremental.add_or_update_root(
                root=self.root,
                source_id="project",
                resume=True,
                reset_clean=True,
            )

    def test_scope_reconciliation_does_not_delete_a_disjoint_scope(self) -> None:
        scope26 = resolve_ingestion_scope(self.root, "plans/FY26")
        scope27 = resolve_ingestion_scope(self.root, "plans/FY27")
        path26 = scope26.file(self.file26).stored_path
        path27 = scope27.file(self.file27).stored_path
        state = {
            "version": 2,
            "files": {
                f"project:{path26}": {
                    "source_id": "project",
                    "stored_path": path26,
                    "resolved_root": str(scope26.resolved_root),
                    "record_ids": ["FY26"],
                },
                f"project:{path27}": {
                    "source_id": "project",
                    "stored_path": path27,
                    "resolved_root": str(scope27.resolved_root),
                    "record_ids": ["FY27"],
                },
            },
        }
        output_root = self.workspace / "db"
        with (
            mock.patch.dict(os.environ, {"RAG_OUTPUT_ROOT": str(output_root)}),
            mock.patch.object(incremental, "delete_ids", return_value=1)
            as delete_ids,
            mock.patch.object(incremental, "delete_catalog_chunks"),
            mock.patch.object(incremental, "emit_event"),
        ):
            result = incremental._reconcile_missing_files(
                state,
                scope=scope27,
                source_id="project",
                discovered_keys=set(),
            )
        self.assertEqual({"deleted_files": 1, "deleted_records": 1}, result)
        delete_ids.assert_called_once_with(["FY27"])
        self.assertIn(f"project:{path26}", state["files"])
        self.assertNotIn(f"project:{path27}", state["files"])

    def test_status_commands_preserve_scope_and_compatibility_flag(self) -> None:
        resume = STATUS._resume_command(
            "project-rag",
            "build",
            str(self.root),
            "project",
            "plans/FY26",
        )
        rebuild = STATUS._force_rebuild_command(
            "project-rag",
            str(self.root),
            "project",
            "plans/FY26",
        )
        for command in (resume, rebuild):
            self.assertIn("--include-root-name-in-path", command)
            index = command.index("--scan-subdir")
            self.assertEqual("plans/FY26", command[index + 1])
        self.assertIn("--resume", resume)
        self.assertIn("--force-rebuild", rebuild)
        with mock.patch.object(STATUS.os, "name", "nt"):
            rendered = STATUS._format_command(resume)
        self.assertTrue(rendered.startswith("& '"))
        self.assertIn("python", rendered.casefold())
        self.assertIn("'plans/FY26'", rendered)

    def test_cli_help_documents_both_compatibility_options(self) -> None:
        for script in ("build_db.py", "add_data.py"):
            completed = subprocess.run(
                [sys.executable, str(GEN_DB_ROOT / script), "--help"],
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("--scan-subdir", completed.stdout)
            self.assertIn(
                "--include-root-name-in-path",
                completed.stdout,
            )
            self.assertIn("always included", completed.stdout)


if __name__ == "__main__":
    unittest.main()
