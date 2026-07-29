from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


RAG_ROOT = Path(__file__).resolve().parents[1]
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

from source_manager import packages  # noqa: E402


class PackageContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / ".copilot"
        self._make_install()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_distribution_is_allowlisted_and_validated(self) -> None:
        source_version = self._read_bytes(
            self.home / "rag/dbs/demo-rag/VERSION.json"
        )
        source_links = self._read_bytes(
            self.home / "rag/dbs/demo-rag/source-links.json"
        )
        source_state = self._read_bytes(
            self.home
            / "rag/dbs/demo-rag/sources/source-a/state.json"
        )
        output = self.root / "distribution.zip"
        created = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)

        result = packages.create_distribution_package(
            self.home,
            output,
            created_at=created,
        )

        self.assertEqual(result["status"], "written")
        manifest = packages.validate_distribution_zip(output)
        paths = {item["path"] for item in manifest["files"]}
        self.assertIn("bootstrap.py", paths)
        self.assertIn(
            ".copilot/rag/gen_db/software_rag_tool/"
            "software_rag_tool/token_budget.py",
            paths,
        )
        self.assertNotIn(
            ".copilot/rag/gen_db/software_rag_tool/"
            "software_rag_tool/incremental.py",
            paths,
        )
        self.assertNotIn(
            ".copilot/rag/gen_db/software_rag_tool/"
            "software_rag_tool/source_delete.py",
            paths,
        )
        self.assertNotIn(
            ".copilot/rag/gen_db/delete_source.py",
            paths,
        )
        self.assertNotIn(".copilot/rag/manage.py", paths)
        self.assertFalse(
            any("/source_manager/" in path for path in paths)
        )
        self.assertFalse(any("/sources/" in path for path in paths))
        self.assertFalse(any("/logs/" in path for path in paths))
        self.assertFalse(any(".venv" in path for path in paths))
        self.assertIn(
            ".copilot/rag/dbs/demo-rag/rag-wrapper.json",
            paths,
        )
        self.assertNotIn(
            ".copilot/rag/dbs/demo-rag/db-snapshot.json",
            paths,
        )
        self.assertEqual(
            manifest["dbs"],
            [
                {
                    "name": "demo-rag",
                    "content_snapshot_at": "2025-01-02T00:00:00Z",
                    "content_snapshot_reason": "rag_wrapper",
                }
            ],
        )
        with zipfile.ZipFile(output) as archive:
            wrapper = json.loads(
                archive.read(
                    ".copilot/rag/dbs/demo-rag/rag-wrapper.json"
                )
            )
            version = json.loads(
                archive.read(".copilot/rag/dbs/demo-rag/VERSION.json")
            )
            copied_links = archive.read(
                ".copilot/rag/dbs/demo-rag/source-links.json"
            )
            catalog_data = archive.read(
                ".copilot/rag/dbs/demo-rag/catalog.sqlite"
            )
        self.assertEqual(
            wrapper["schema_version"],
            "local-rag.wrapper.v1",
        )
        self.assertEqual(
            wrapper["content_snapshot_at"],
            "2025-01-02T00:00:00Z",
        )
        self.assertEqual(wrapper["packaged_at"], "2026-01-02T03:04:00Z")
        self.assertEqual(
            version,
            json.loads(source_version.decode("utf-8")),
        )
        self.assertEqual(copied_links, source_links)
        catalog_copy = self.root / "catalog-copy.sqlite"
        catalog_copy.write_bytes(catalog_data)
        with closing(sqlite3.connect(catalog_copy)) as connection:
            self.assertEqual(
                connection.execute("SELECT value FROM marker").fetchone(),
                ("ready",),
            )
        self.assertEqual(
            self._read_bytes(self.home / "rag/dbs/demo-rag/VERSION.json"),
            source_version,
        )
        self.assertEqual(
            self._read_bytes(
                self.home
                / "rag/dbs/demo-rag/sources/source-a/state.json"
            ),
            source_state,
        )
        self.assertFalse(
            any(
                path.suffix in {".lock", ".tmp"}
                for path in self.home.rglob("*")
            )
        )
        extracted = self.root / "extracted"
        with zipfile.ZipFile(output) as archive:
            archive.extractall(extracted)
        installed = self.root / "installed"
        completed = subprocess.run(
            [
                sys.executable,
                str(extracted / "bootstrap.py"),
                str(installed),
                "--skip-dependencies",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue((installed / "rag/query/search.py").is_file())
        runtime = (
            installed / "rag/query/.venv/Scripts/python.exe"
            if os.name == "nt"
            else installed / "rag/query/.venv/bin/python"
        )
        self.assertTrue(runtime.is_file())

    def test_repack_preserves_content_snapshot_and_only_advances_package_time(
        self,
    ) -> None:
        first = self.root / "first.zip"
        packages.create_distribution_package(
            self.home,
            first,
            created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        extracted = self.root / "repack-source"
        with zipfile.ZipFile(first) as archive:
            archive.extractall(extracted)

        second = self.root / "second.zip"
        packages.create_distribution_package(
            extracted / ".copilot",
            second,
            created_at=datetime(2026, 2, 3, tzinfo=timezone.utc),
        )
        with zipfile.ZipFile(second) as archive:
            wrapper = json.loads(
                archive.read(
                    ".copilot/rag/dbs/demo-rag/rag-wrapper.json"
                )
            )
        self.assertEqual(
            "2025-01-02T00:00:00Z",
            wrapper["content_snapshot_at"],
        )
        self.assertEqual(
            "2026-02-03T00:00:00Z",
            wrapper["packaged_at"],
        )

    def test_missing_wrapper_stays_unknown_and_never_uses_legacy_dates(
        self,
    ) -> None:
        (self.home / "rag/dbs/demo-rag/rag-wrapper.json").unlink()
        output = self.root / "unknown-freshness.zip"
        result = packages.create_distribution_package(
            self.home,
            output,
            created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        self.assertEqual(
            [
                {
                    "name": "demo-rag",
                    "content_snapshot_at": None,
                    "content_snapshot_reason": "unknown",
                }
            ],
            result["manifest"]["dbs"],
        )
        with zipfile.ZipFile(output) as archive:
            wrapper = json.loads(
                archive.read(
                    ".copilot/rag/dbs/demo-rag/rag-wrapper.json"
                )
            )
        self.assertIsNone(wrapper["content_snapshot_at"])
        self.assertEqual("2026-01-02T00:00:00Z", wrapper["packaged_at"])

    @unittest.skipIf(
        not hasattr(os, "symlink"),
        "symbolic links unavailable",
    )
    def test_bootstrap_rejects_symlinked_install_parent(self) -> None:
        archive_path = self.root / "symlink-bootstrap.zip"
        packages.create_distribution_package(self.home, archive_path)
        extracted = self.root / "symlink-bootstrap"
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(extracted)
        target = self.root / "install-target"
        outside = self.root / "outside"
        target.mkdir()
        outside.mkdir()
        try:
            os.symlink(outside, target / "rag", target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symbolic links unavailable: {type(exc).__name__}")
        completed = subprocess.run(
            [
                sys.executable,
                str(extracted / "bootstrap.py"),
                str(target),
                "--skip-dependencies",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertNotEqual(0, completed.returncode)
        self.assertFalse(any(outside.iterdir()))

    def test_bootstrap_safely_replaces_existing_database_only(
        self,
    ) -> None:
        archive_path = self.root / "existing-db.zip"
        packages.create_distribution_package(self.home, archive_path)
        extracted = self.root / "existing-db-package"
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(extracted)
        target = self.root / "existing-db-target"
        existing = target / "rag/dbs/demo-rag"
        existing.mkdir(parents=True)
        sentinel = existing / "sentinel.txt"
        sentinel.write_text("keep", encoding="utf-8")
        unrelated_db = target / "rag/dbs/unrelated-rag"
        unrelated_db.mkdir(parents=True)
        unrelated_sentinel = unrelated_db / "sentinel.txt"
        unrelated_sentinel.write_text("unrelated", encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                str(extracted / "bootstrap.py"),
                str(target),
                "--skip-dependencies",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertFalse(sentinel.exists())
        self.assertTrue((existing / "VERSION.json").is_file())
        self.assertEqual(
            "unrelated",
            unrelated_sentinel.read_text(encoding="utf-8"),
        )
        self.assertEqual(
            [],
            list((target / "rag/dbs").glob(".*.previous")),
        )

    def test_staged_database_validation_failure_preserves_existing(
        self,
    ) -> None:
        output = self.root / "invalid-staged-admin"
        packages.create_admin_transfer_package(self.home, output)
        spec = importlib.util.spec_from_file_location(
            "generated_invalid_stage_bootstrap",
            output / "bootstrap.py",
        )
        assert spec is not None and spec.loader is not None
        bootstrap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bootstrap)
        shutil.rmtree(output / "__pycache__", ignore_errors=True)
        target = self.root / "invalid-stage-target"
        existing = target / "rag/dbs/demo-rag"
        existing.mkdir(parents=True)
        sentinel = existing / "sentinel.txt"
        sentinel.write_text("original", encoding="utf-8")
        original_copy = bootstrap.copy_atomic
        corrupted = False

        def corrupt_staged_copy(source: Path, destination: Path) -> None:
            nonlocal corrupted
            original_copy(source, destination)
            if (
                not corrupted
                and any(
                    part.endswith(".incoming")
                    for part in Path(destination).parts
                )
            ):
                Path(destination).write_bytes(b"corrupted")
                corrupted = True

        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    str(output / "bootstrap.py"),
                    str(target),
                    "--skip-runtime-setup",
                ],
            ),
            mock.patch.object(
                bootstrap,
                "copy_atomic",
                side_effect=corrupt_staged_copy,
            ),
            self.assertRaises(SystemExit),
        ):
            bootstrap.main()
        self.assertTrue(corrupted)
        self.assertEqual("original", sentinel.read_text(encoding="utf-8"))
        self.assertEqual(
            [],
            list((target / "rag/dbs").glob(".*.previous")),
        )

    def test_manifest_cannot_write_an_undeclared_database(self) -> None:
        manifest = {
            "schema": packages.PACKAGE_SCHEMA,
            "kind": "distribution",
            "created": "2026-01-01T00:00:00Z",
            "tool": {
                "name": packages.PACKAGE_TOOL_NAME,
                "version": "1.0.0",
            },
            "dbs": [
                {
                    "name": "declared-rag",
                    "content_snapshot_at": (
                        "2026-01-01T00:00:00Z"
                    ),
                    "content_snapshot_reason": "full_update",
                }
            ],
            "files": [
                {
                    "path": ".copilot/rag/dbs/unrelated-rag/db.json",
                    "size": 0,
                    "sha256": "0" * 64,
                }
            ],
            "total": {"files": 1, "bytes": 0},
        }
        with self.assertRaisesRegex(
            packages.PackageError,
            "package_manifest_dbs_invalid",
        ):
            packages._validate_manifest_shape(
                manifest,
                expected_kind="distribution",
            )

    def test_import_package_uses_safe_bootstrap_without_runtime_changes(
        self,
    ) -> None:
        output = self.root / "manager-import"
        packages.create_admin_transfer_package(self.home, output)
        target = self.root / "manager-target"
        existing = target / "rag/dbs/demo-rag"
        existing.mkdir(parents=True)
        (existing / "sentinel.txt").write_text("old", encoding="utf-8")

        result = packages.import_package(output, target)

        self.assertEqual("imported", result["status"])
        self.assertEqual(["demo-rag"], result["databases"])
        self.assertFalse((existing / "sentinel.txt").exists())
        self.assertTrue((existing / "db.json").is_file())
        self.assertFalse((target / "rag/query/.venv").exists())

    def test_bootstrap_rolls_back_all_databases_on_publish_failure(
        self,
    ) -> None:
        source_db = self.home / "rag/dbs/demo-rag"
        second_db = self.home / "rag/dbs/second-rag"
        shutil.copytree(source_db, second_db)
        shutil.rmtree(second_db / "logs")
        output = self.root / "multi-db-admin"
        packages.create_admin_transfer_package(self.home, output)
        spec = importlib.util.spec_from_file_location(
            "generated_local_rag_bootstrap",
            output / "bootstrap.py",
        )
        assert spec is not None and spec.loader is not None
        bootstrap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bootstrap)
        shutil.rmtree(output / "__pycache__", ignore_errors=True)
        target = self.root / "multi-db-target"
        for name, marker in (
            ("demo-rag", "old-demo"),
            ("second-rag", "old-second"),
            ("unrelated-rag", "unrelated"),
        ):
            root = target / "rag/dbs" / name
            root.mkdir(parents=True)
            (root / "sentinel.txt").write_text(marker, encoding="utf-8")
        original_replace = os.replace
        published = 0

        def fail_second_publish(source: object, destination: object) -> None:
            nonlocal published
            source_path = Path(source)
            destination_path = Path(destination)
            if (
                source_path.name.endswith(".incoming")
                and destination_path.parent.name == "dbs"
            ):
                published += 1
                if published == 2:
                    raise OSError("synthetic publish failure")
            original_replace(source, destination)

        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    str(output / "bootstrap.py"),
                    str(target),
                    "--skip-dependencies",
                ],
            ),
            mock.patch.object(
                bootstrap.os,
                "replace",
                side_effect=fail_second_publish,
            ),
            self.assertRaises(OSError),
        ):
            bootstrap.main()
        installed_dbs = target / "rag/dbs"
        self.assertEqual(
            "old-demo",
            (installed_dbs / "demo-rag/sentinel.txt").read_text(
                encoding="utf-8"
            ),
        )
        self.assertEqual(
            "old-second",
            (installed_dbs / "second-rag/sentinel.txt").read_text(
                encoding="utf-8"
            ),
        )
        self.assertEqual(
            "unrelated",
            (installed_dbs / "unrelated-rag/sentinel.txt").read_text(
                encoding="utf-8"
            ),
        )
        self.assertEqual([], list(installed_dbs.glob(".*.previous")))

    def test_admin_transfer_preserves_state_and_makes_add_paths_portable(
        self,
    ) -> None:
        output = self.root / "admin-transfer"
        business_record = (
            self.home
            / "rag/dbs/demo-rag/data/clean/business.json"
        )
        business_record.write_text(
            '{"text":"/business/reference/must-stay"}\n',
            encoding="utf-8",
        )
        fetched_json = (
            self.home
            / "rag/dbs/demo-rag/sources/source-a/work/document.json"
        )
        fetched_json.write_text(
            '{"text":"C:\\\\business\\\\reference\\\\must-stay"}\n',
            encoding="utf-8",
        )
        source_state_path = (
            self.home
            / "rag/dbs/demo-rag/sources/source-a/state.json"
        )
        source_state_path.write_text(
            json.dumps(
                {
                    "status": "interrupted",
                    "runtime": {
                        "input_path": str(
                            self.root / "operator-selected-input"
                        )
                    },
                }
            ),
            encoding="utf-8",
        )
        original_wrapper = self._read_bytes(
            self.home / "rag/dbs/demo-rag/rag-wrapper.json"
        )
        source_state = self._read_bytes(
            self.home
            / "rag/dbs/demo-rag/sources/source-a/state.json"
        )

        result = packages.create_admin_transfer_package(
            self.home,
            output,
        )

        self.assertEqual(result["kind"], "admin-transfer")
        manifest = packages.validate_package_tree(
            output,
            expected_kind="admin-transfer",
        )
        self.assertEqual(
            [
                {
                    "name": "demo-rag",
                    "content_snapshot_at": "2025-01-02T00:00:00Z",
                    "content_snapshot_reason": "rag_wrapper",
                }
            ],
            manifest["dbs"],
        )
        paths = {item["path"] for item in manifest["files"]}
        self.assertIn(".copilot/rag/manage.py", paths)
        self.assertIn(
            ".copilot/rag/source_manager/store.py",
            paths,
        )
        self.assertIn(".copilot/rag/gen_db/add_data.py", paths)
        self.assertIn(".copilot/rag/gen_db/delete_source.py", paths)
        self.assertIn(
            ".copilot/rag/gen_db/software_rag_tool/"
            "software_rag_tool/source_delete.py",
            paths,
        )
        self.assertNotIn(
            ".copilot/skills/local-rag-admin/SKILL.md",
            paths,
        )
        self.assertIn(
            ".copilot/rag/dbs/demo-rag/sources/source-a/work/"
            ".git/config",
            paths,
        )
        self.assertIn(
            ".copilot/rag/dbs/demo-rag/sources/source-a/work/"
            ".svn/entries",
            paths,
        )
        self.assertEqual(
            self._read_bytes(
                output / ".copilot/rag/dbs/demo-rag/rag-wrapper.json"
            ),
            original_wrapper,
        )
        progress = json.loads(
            (
                output
                / ".copilot/rag/dbs/demo-rag/logs/progress.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            progress["root"],
            {
                "__local_rag_db_relative_path__":
                "sources/source-a/work/docs"
            },
        )
        index_state = json.loads(
            (
                output
                / ".copilot/rag/dbs/demo-rag/logs/index_state.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            {
                "__local_rag_db_relative_path__":
                "sources/source-a/work"
            },
            index_state["ingestion"]["resolved_root"],
        )
        event = json.loads(
            (
                output
                / ".copilot/rag/dbs/demo-rag/logs/events.jsonl"
            ).read_text(encoding="utf-8")
        )
        self.assertTrue(event["temporary"].startswith("<TEMP_ROOT>/"))
        portable_state = json.loads(
            (
                output
                / ".copilot/rag/dbs/demo-rag/sources/source-a/state.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            "<INPUT_RESELECT_REQUIRED>",
            portable_state["runtime"]["input_path"],
        )
        self.assertEqual(
            business_record.read_bytes(),
            (
                output
                / ".copilot/rag/dbs/demo-rag/data/clean/business.json"
            ).read_bytes(),
        )
        self.assertEqual(
            fetched_json.read_bytes(),
            (
                output
                / ".copilot/rag/dbs/demo-rag/sources/source-a/"
                "work/document.json"
            ).read_bytes(),
        )
        self.assertEqual(
            self._read_bytes(
                self.home
                / "rag/dbs/demo-rag/sources/source-a/state.json"
            ),
            source_state,
        )

        installed = self.root / "installed-admin"
        completed = subprocess.run(
            [
                sys.executable,
                str(output / "bootstrap.py"),
                str(installed),
                "--skip-dependencies",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        installed_db = installed / "rag/dbs/demo-rag"
        restored_progress = json.loads(
            (installed_db / "logs/progress.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            str(installed_db / "sources/source-a/work/docs"),
            restored_progress["root"],
        )
        restored_index = json.loads(
            (installed_db / "logs/index_state.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            str(installed_db / "sources/source-a/work"),
            restored_index["ingestion"]["resolved_root"],
        )

    def test_admin_transfer_rejects_private_key_names_and_headers(
        self,
    ) -> None:
        work = (
            self.home
            / "rag/dbs/demo-rag/sources/source-a/work"
        )
        named_key = work / "id_ed25519"
        named_key.write_text("not a real key", encoding="utf-8")
        with self.assertRaisesRegex(
            packages.PackageError,
            "forbidden_package_source",
        ):
            packages.create_admin_transfer_package(
                self.home,
                self.root / "admin-private-name",
            )
        named_key.unlink()
        header_key = work / "innocent.txt"
        header_key.write_text(
            "-----BEGIN OPENSSH PRIVATE KEY-----\nredacted\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            packages.PackageError,
            "forbidden_package_source",
        ):
            packages.create_admin_transfer_package(
                self.home,
                self.root / "admin-private-header",
            )

    def test_sharepoint_add_state_is_rebound_to_destination_environment(
        self,
    ) -> None:
        database = self.home / "rag/dbs/demo-rag"
        source_directory = database / "sources/source-a"
        source_config = {
            "schema_version": "local-rag-source-manager-v1",
            "local_source_key": "source-a",
            "source_id": "fixture-source",
            "source_type": "sharepoint",
            "display_name": "SharePoint fixture",
            "fetch": {
                "root_env": "LOCAL_RAG_SHAREPOINT_ROOT",
                "relative_path": "Team/Docs",
            },
            "ingest": {
                "work_directory": "sources/source-a/work/ingest/source-a",
                "logical_root_name": "source-a",
            },
        }
        self._write(
            source_directory / "source.json",
            json.dumps(source_config),
        )
        old_environment_root = self.root / "old-sharepoint"
        old_source_root = old_environment_root / "Team/Docs"
        self._write(old_source_root / "manual.txt", "old synced file")
        progress_file = database / "logs/progress.json"
        index_file = database / "logs/index_state.json"
        self._write(
            progress_file,
            json.dumps(
                {
                    "source_id": "fixture-source",
                    "root": str(old_source_root),
                    "scan_root": str(old_source_root),
                }
            ),
        )
        self._write(
            index_file,
            json.dumps(
                {
                    "ingestion": {
                        "source_id": "fixture-source",
                        "resolved_root": str(old_source_root),
                    }
                }
            ),
        )
        output = self.root / "sharepoint-admin-transfer"
        with mock.patch.dict(
            os.environ,
            {"LOCAL_RAG_SHAREPOINT_ROOT": str(old_environment_root)},
        ):
            packages.create_admin_transfer_package(self.home, output)

        portable_progress = json.loads(
            (
                output
                / ".copilot/rag/dbs/demo-rag/logs/progress.json"
            ).read_text(encoding="utf-8")
        )
        expected_marker = {
            "__local_rag_sharepoint_source_key__": "source-a",
            "source_relative_suffix": "",
        }
        self.assertEqual(expected_marker, portable_progress["root"])
        self.assertEqual(expected_marker, portable_progress["scan_root"])
        self.assertNotIn(
            str(old_environment_root),
            (
                output
                / ".copilot/rag/dbs/demo-rag/logs/progress.json"
            ).read_text(encoding="utf-8"),
        )

        new_environment_root = self.root / "new-sharepoint"
        new_source_root = new_environment_root / "Team/Docs"
        new_source_root.mkdir(parents=True)
        installed = self.root / "installed-sharepoint-admin"
        environment = os.environ.copy()
        environment["LOCAL_RAG_SHAREPOINT_ROOT"] = str(
            new_environment_root
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(output / "bootstrap.py"),
                str(installed),
                "--skip-dependencies",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        restored_progress = json.loads(
            (
                installed
                / "rag/dbs/demo-rag/logs/progress.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(str(new_source_root), restored_progress["root"])
        self.assertEqual(
            str(new_source_root),
            restored_progress["scan_root"],
        )

    def test_admin_transfer_resumes_and_skips_verified_files(self) -> None:
        output = self.root / "resumable-admin-transfer"
        created = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
        original_atomic = packages._atomic_bytes
        written: list[Path] = []

        def interrupt(path: Path, payload: bytes) -> None:
            if len(written) == 4:
                raise KeyboardInterrupt()
            original_atomic(path, payload)
            written.append(path)

        with mock.patch.object(packages, "_atomic_bytes", interrupt):
            with self.assertRaises(KeyboardInterrupt):
                packages.create_admin_transfer_package(
                    self.home,
                    output,
                    created_at=created,
                )
        partial = output.parent / f".{output.name}.partial"
        self.assertTrue(partial.is_dir())
        retained = written[0]
        retained_relative = retained.resolve().relative_to(partial.resolve())
        before = (retained.stat().st_mtime_ns, packages._sha256(retained))

        result = packages.create_admin_transfer_package(
            self.home,
            output,
            created_at=created,
        )

        self.assertEqual("written", result["status"])
        final_retained = output / retained_relative
        self.assertEqual(
            before,
            (
                final_retained.stat().st_mtime_ns,
                packages._sha256(final_retained),
            ),
        )
        self.assertFalse(partial.exists())
        self.assertEqual(
            "already_complete",
            packages.create_admin_transfer_package(
                self.home,
                output,
                created_at=created,
            )["status"],
        )

    @unittest.skipIf(
        not hasattr(os, "symlink"),
        "symbolic links unavailable",
    )
    def test_admin_resume_rejects_symlinked_partial_tree(self) -> None:
        output = self.root / "unsafe-resume"
        partial = output.parent / f".{output.name}.partial"
        partial.mkdir()
        outside = self.root / "outside-resume"
        outside.mkdir()
        try:
            os.symlink(
                outside,
                partial / ".copilot",
                target_is_directory=True,
            )
        except OSError as exc:
            self.skipTest(f"symbolic links unavailable: {type(exc).__name__}")
        with self.assertRaisesRegex(
            packages.PackageError,
            "package_resume_symlink_forbidden",
        ):
            packages.create_admin_transfer_package(self.home, output)
        self.assertFalse(any(outside.iterdir()))

    def test_validator_rejects_unmanifested_and_changed_files(self) -> None:
        output = self.root / "admin-transfer"
        packages.create_admin_transfer_package(self.home, output)
        unmanifested = output / "unexpected.txt"
        unmanifested.write_text("unexpected", encoding="utf-8")
        with self.assertRaisesRegex(
            packages.PackageError,
            "package_manifest_coverage_mismatch",
        ):
            packages.validate_package_tree(output)
        unmanifested.unlink()
        target = output / ".copilot/rag/README.md"
        target.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(
            packages.PackageError,
            "package_checksum_mismatch",
        ):
            packages.validate_package_tree(output)

    def test_validator_rejects_manifest_traversal(self) -> None:
        output = self.root / "admin-transfer"
        packages.create_admin_transfer_package(self.home, output)
        manifest_path = output / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][0]["path"] = "../outside"
        manifest_path.write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            packages.PackageError,
            "package_path_invalid",
        ):
            packages.validate_package_tree(output)

    @unittest.skipIf(
        not hasattr(os, "symlink"),
        "symbolic links are unavailable",
    )
    def test_source_symlink_is_rejected(self) -> None:
        target = self.home / "rag/dbs/demo-rag/index/value.bin"
        target.unlink()
        target.symlink_to(self.home / "rag/VERSION")
        with self.assertRaisesRegex(
            packages.PackageError,
            "package_symlink_forbidden",
        ):
            packages.create_distribution_package(
                self.home,
                self.root / "distribution.zip",
            )

    def test_git_credentials_are_rejected_without_value_disclosure(
        self,
    ) -> None:
        secret = "do-not-disclose"
        work = (
            self.home
            / "rag/dbs/demo-rag/sources/source-a/work"
        )
        cases = {
            work / ".git/config": (
                "[remote \"origin\"]\n"
                f"url = https://user:{secret}@example.invalid/repository\n"
            ),
            work / ".lfsconfig": (
                "[lfs]\n"
                f"url = https://user:{secret}@example.invalid/lfs\n"
            ),
            work / ".gitmodules": (
                "[submodule \"fixture\"]\n"
                "path = fixture\n"
                f"url = https://user:{secret}@example.invalid/module\n"
            ),
        }
        for index, (config, value) in enumerate(cases.items()):
            with self.subTest(config=config.name):
                config.write_text(value, encoding="utf-8")
                with self.assertRaises(packages.PackageError) as context:
                    packages.create_admin_transfer_package(
                        self.home,
                        self.root / f"admin-transfer-{index}",
                    )
                self.assertEqual(
                    str(context.exception),
                    "credential_configuration_detected",
                )
                self.assertNotIn(secret, str(context.exception))
                config.unlink()

    def test_source_link_credentials_are_rejected(self) -> None:
        sidecar = self.home / "rag/dbs/demo-rag/source-links.json"
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        payload["sources"][0]["link"]["settings"]["access_token"] = "hidden"
        sidecar.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(
            packages.PackageError,
            "credential_configuration_detected",
        ):
            packages.create_distribution_package(
                self.home,
                self.root / "distribution.zip",
            )

    def test_distribution_requires_current_source_metadata_schema(self) -> None:
        sidecar = self.home / "rag/dbs/demo-rag/source-links.json"
        sidecar.write_text(
            json.dumps(
                {
                    "schema_version": "rag-source-links-v2",
                    "revision": 1,
                    "sources": [],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            packages.PackageError,
            "source_links_not_current",
        ):
            packages.create_distribution_package(
                self.home,
                self.root / "distribution.zip",
            )

    def test_secret_file_in_selected_tree_is_rejected(self) -> None:
        self._write(
            self.home / "rag/models/model/credentials.json",
            '{"secret":"hidden"}',
        )
        with self.assertRaisesRegex(
            packages.PackageError,
            "forbidden_package_source",
        ):
            packages.create_distribution_package(
                self.home,
                self.root / "distribution.zip",
            )

    def test_machine_temp_parent_does_not_hide_normal_sources(self) -> None:
        source_root = (
            self.root / "Temp" / ".copilot" / "rag" / "package-input"
        )
        ordinary = source_root / "nested" / "document.txt"
        self._write(ordinary, "ordinary")
        entries: list[packages._Entry] = []
        packages._add_file(
            entries,
            ordinary,
            ".copilot/rag/document.txt",
            required=True,
        )
        packages._add_tree(
            entries,
            source_root,
            ".copilot/rag/tree",
            include=lambda _path: True,
        )
        destinations = {entry.destination for entry in entries}
        self.assertIn(".copilot/rag/document.txt", destinations)
        self.assertIn(
            ".copilot/rag/tree/nested/document.txt",
            destinations,
        )

    def test_ambiguous_add_state_fails_closed(self) -> None:
        source_a = (
            self.home / "rag/dbs/demo-rag/sources/source-a/source.json"
        )
        source_b = (
            self.home / "rag/dbs/demo-rag/sources/source-b/source.json"
        )
        source_b.parent.mkdir(parents=True)
        source_b.write_bytes(source_a.read_bytes())
        (source_b.parent / "state.json").write_text(
            '{"status":"ready"}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            packages.PackageError,
            "portable_add_state_ambiguous",
        ):
            packages.create_admin_transfer_package(
                self.home,
                self.root / "admin-transfer",
            )

    def test_source_fingerprint_change_is_rejected(self) -> None:
        source = self.home / "rag/README.md"
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        source.write_text("changed after observation", encoding="utf-8")
        entry = packages._Entry(source, ".copilot/rag/README.md")
        with self.assertRaisesRegex(
            packages.PackageError,
            "package_source_changed",
        ):
            packages._verify_source_fingerprints([(entry, before)])

    def test_distribution_entrypoint_returns_json(self) -> None:
        output = self.root / "entrypoint.zip"
        completed = subprocess.run(
            [
                sys.executable,
                str(RAG_ROOT / "make_distribution_package.py"),
                "--copilot-home",
                str(self.home),
                "--output",
                str(output),
                "--db",
                "demo-rag",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "written")
        self.assertTrue(output.is_file())

    def test_admin_entrypoint_returns_json(self) -> None:
        output = self.root / "entrypoint-admin"
        completed = subprocess.run(
            [
                sys.executable,
                str(RAG_ROOT / "make_admin_transfer_package.py"),
                "--copilot-home",
                str(self.home),
                "--output",
                str(output),
                "--db",
                "demo-rag",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["kind"], "admin-transfer")
        self.assertTrue((output / "manifest.json").is_file())

    def test_distribution_tool_allowlist_covers_import_closure(self) -> None:
        module_root = (
            RAG_ROOT
            / "gen_db/software_rag_tool/software_rag_tool"
        )
        allowed = set(packages._DISTRIBUTION_TOOL_MODULES)
        for name in sorted(allowed):
            path = module_root / name
            tree = ast.parse(path.read_text(encoding="utf-8"))
            dependencies: set[str] = set()
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or node.level != 1:
                    continue
                if node.module:
                    dependencies.add(node.module.split(".", 1)[0] + ".py")
                else:
                    dependencies.update(
                        alias.name.split(".", 1)[0] + ".py"
                        for alias in node.names
                    )
            dependencies = {
                dependency
                for dependency in dependencies
                if (module_root / dependency).is_file()
            }
            self.assertTrue(
                dependencies <= allowed,
                f"{name} has unallowlisted imports: "
                f"{sorted(dependencies - allowed)}",
            )

    def _make_install(self) -> None:
        rag = self.home / "rag"
        for name in packages._RAG_DISTRIBUTION_FILES:
            self._write(rag / name, "fixture\n")
        self._write(rag / "VERSION", "9.9.9\n")
        self._write(rag / "manage.py", "print('manager')\n")
        self._write(rag / "wrapper/__init__.py", "")
        self._write(rag / "wrapper/search_command.py", "")
        for name in packages._QUERY_DISTRIBUTION_FILES:
            self._write(rag / "query" / name, "fixture\n")
        tool = rag / "gen_db/software_rag_tool"
        self._write(tool / "pyproject.toml", "[project]\nname='fixture'\n")
        self._write(tool / "requirements.txt", "")
        for name in packages._DISTRIBUTION_TOOL_MODULES:
            self._write(tool / "software_rag_tool" / name, "")
        for name in packages._ADMIN_TOOL_MODULES:
            self._write(tool / "software_rag_tool" / name, "")
        self._write(
            tool / "software_rag_tool/incremental.py",
            "raise RuntimeError('admin only')\n",
        )
        self._write(rag / "gen_db/README.md", "fixture\n")
        for name in packages._ADMIN_GEN_DB_FILES - {"README.md"}:
            self._write(rag / "gen_db" / name, "fixture\n")
        self._write(rag / "source_manager/__init__.py", "")
        self._write(rag / "source_manager/store.py", "")
        self._write(rag / "source_manager/tests/test_hidden.py", "")
        self._write(
            self.home / "instructions/rag.instructions.md",
            "# Routing\n",
        )
        self._write(
            self.home / "skills/local-rag/SKILL.md",
            "# Lookup\n",
        )
        self._write(
            self.home / "skills/local-rag-admin/SKILL.md",
            "# Administration\n",
        )
        self._write(rag / "models/model/model.onnx", "model")
        db = rag / "dbs/demo-rag"
        self._write(db / "db.json", '{"title":"Fixture"}\n')
        self._write(db / "DB_PROFILE.md", "# Fixture\n")
        self._write(
            db / "VERSION.json",
            '{"created_at":"2025-01-01T00:00:00Z",'
            '"snapshot_reason":"original"}\n',
        )
        self._write(
            db / "rag-wrapper.json",
            '{"schema_version":"local-rag.wrapper.v1",'
            '"content_snapshot_at":"2025-01-02T00:00:00Z",'
            '"packaged_at":"2025-01-03T00:00:00Z"}\n',
        )
        self._write(
            db / "db-snapshot.json",
            '{"schema_version":"local-rag-db-snapshot-v1",'
            '"snapshot_at":"2025-01-02T00:00:00Z",'
            '"reason":"full_update","marker":"preserve-me"}\n',
        )
        self._write(
            db / "source-links.json",
            '{"schema_version":"rag-source-metadata-v1","revision":1,'
            '"sources":[{"source_id":"fixture-source",'
            '"display_name":"Fixture source","source_type":"github",'
            '"link":{"enabled":true,"strategy":"github-blob","settings":{'
            '"repository_url":"https://example.invalid/repository",'
            '"ref":"fixture","repository_path_prefix":"",'
            '"permalink_enabled":false}}}]}\n',
        )
        self._write(db / "index/value.bin", "index")
        with closing(sqlite3.connect(db / "catalog.sqlite")) as connection:
            connection.execute("CREATE TABLE marker(value TEXT)")
            connection.execute("INSERT INTO marker VALUES ('ready')")
            connection.commit()
        source_dir = db / "sources/source-a"
        self._write(
            source_dir / "source.json",
            '{"source_id":"fixture-source",'
            '"work_path":"sources/source-a/work"}\n',
        )
        self._write(source_dir / "state.json", '{"status":"running"}\n')
        self._write(source_dir / "events.jsonl", '{"event":"ready"}\n')
        work = source_dir / "work"
        self._write(work / "docs/input.txt", "source content")
        self._write(
            work / ".git/config",
            '[remote "origin"]\n'
            "url = https://example.invalid/repository\n",
        )
        self._write(work / ".svn/entries", "fixture\n")
        self._write(db / "data/clean/record.jsonl", '{"text":"fixture"}\n')
        self._write(
            db / "logs/progress.json",
            json.dumps(
                {
                    "operation": "add",
                    "source_id": "fixture-source",
                    "root": str(work / "docs"),
                    "scan_root": str(work / "docs"),
                    "status": "running",
                }
            ),
        )
        self._write(
            db / "logs/index_state.json",
            json.dumps(
                {
                    "version": 2,
                    "ingestion": {
                        "source_id": "fixture-source",
                        "resolved_root": str(work),
                        "scan_subdir": ".",
                    },
                    "files": {
                        "fixture-source:docs/input.txt": {
                            "source_id": "fixture-source",
                            "stored_path": "work/docs/input.txt",
                            "resolved_root": str(work),
                            "records_path": "records/fixture/one.jsonl",
                            "record_ids": ["fixture-record"],
                            "content_hash": "fixture-hash",
                        }
                    },
                }
            ),
        )
        self._write(
            db / "logs/events.jsonl",
            json.dumps(
                {
                    "event": "status",
                    "temporary": str(
                        Path(tempfile.gettempdir())
                        / "fixture-operation"
                    ),
                }
            )
            + "\n",
        )

    @staticmethod
    def _write(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    @staticmethod
    def _read_bytes(path: Path) -> bytes:
        return path.read_bytes()


if __name__ == "__main__":
    unittest.main()
