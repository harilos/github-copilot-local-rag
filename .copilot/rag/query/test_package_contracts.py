from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


RAG_ROOT = Path(__file__).resolve().parents[1]
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

from setup_copy import restore_copied_installation  # noqa: E402
from source_manager import packages  # noqa: E402
from source_manager.copy_only_packages import _without_bootstrap  # noqa: E402


class PackageContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_copy_only_runtime_removes_generated_bootstrap_entry(self) -> None:
        entries = [
            SimpleNamespace(destination=".copilot/rag/search.py", mode="copy"),
            SimpleNamespace(destination="bootstrap.py", mode="bootstrap"),
        ]
        filtered = _without_bootstrap(entries)
        self.assertEqual(1, len(filtered))
        self.assertEqual(".copilot/rag/search.py", filtered[0].destination)

    def test_default_distribution_contains_one_personal_skill(self) -> None:
        copilot_home = Path(__file__).resolve().parents[2]
        generated, _databases = packages._distribution_entries(
            copilot_home,
            db_names=None,
        )
        destinations = {entry.destination for entry in generated}
        self.assertIn(
            ".copilot/rag/setup_copy.py",
            destinations,
        )
        self.assertIn("install.sh", destinations)
        self.assertIn("install.ps1", destinations)
        self.assertFalse(
            any(destination.startswith(".copilot/agents/") for destination in destinations)
        )
        self.assertIn(".copilot/skills/local-rag/SKILL.md", destinations)
        self.assertNotIn(
            ".copilot/skills/local-rag-setup/SKILL.md",
            destinations,
        )
        self.assertFalse(
            any(
                destination.startswith(".copilot/instructions/")
                for destination in destinations
            )
        )
        self.assertFalse(
            any(
                destination.startswith(".copilot/rag/copilot-cli/")
                for destination in destinations
            )
        )
        for skill_runtime in (
            "agent003_answer_packet.py",
            "result_bundle.py",
            "result_gateway.py",
            "skill_runner.py",
        ):
            self.assertIn(
                f".copilot/rag/query/{skill_runtime}",
                destinations,
            )
        self.assertNotIn(".copilot/rag/query/mcp_server.py", destinations)

    def test_explicit_empty_database_selection_never_expands_to_all(self) -> None:
        dbs = self.root / "dbs"
        (dbs / "one-rag").mkdir(parents=True)
        (dbs / "one-rag" / "db.json").write_text("{}", encoding="utf-8")
        self.assertEqual([], packages._selected_database_names(dbs, ()))
        self.assertEqual(["one-rag"], packages._selected_database_names(dbs, None))
        with self.assertRaisesRegex(packages.PackageError, "database_name_duplicate"):
            packages._selected_database_names(dbs, ("one-rag", "ONE-RAG"))

    def test_distribution_excludes_completion_markers_and_backups(self) -> None:
        for relative in (
            "rag/query/.rag-deps-installed",
            "rag/query/.rag-deps-installed.active.pre-update.123",
        ):
            self.assertTrue(packages._is_transient_path(Path(relative)))

    def test_manager_import_accepts_package_without_bootstrap(self) -> None:
        package = self.root / "package"
        source = self.root / "source"
        source.mkdir()
        setup = source / "setup.py"
        setup.write_text("print('setup')\n", encoding="utf-8")
        db_config = source / "db.json"
        db_config.write_text('{"db_name":"demo-rag"}\n', encoding="utf-8")
        version = source / "VERSION.json"
        version.write_text('{"schema":"local-rag.db-version.v1"}\n', encoding="utf-8")
        catalog = source / "catalog.sqlite"
        catalog.write_bytes(b"catalog")

        entries = [
            packages._Entry(None, "install.sh", mode="install_sh"),
            packages._Entry(None, "install.ps1", mode="install_ps1"),
            packages._Entry(setup, ".copilot/rag/query/setup.py"),
            packages._Entry(db_config, ".copilot/rag/dbs/demo-rag/db.json"),
            packages._Entry(version, ".copilot/rag/dbs/demo-rag/VERSION.json"),
            packages._Entry(catalog, ".copilot/rag/dbs/demo-rag/catalog.sqlite"),
        ]
        packages._stage_package(
            package,
            entries,
            kind=packages._DISTRIBUTION_KIND,
            databases=[
                {
                    "name": "demo-rag",
                    "content_snapshot_at": None,
                    "content_snapshot_reason": "unknown",
                }
            ],
            created="2026-07-30T00:00:00Z",
            tool_version="test",
        )
        packages.validate_package_tree(package)
        self.assertFalse((package / "bootstrap.py").exists())

        target = self.root / ".copilot"
        old_db = target / "rag/dbs/demo-rag"
        old_db.mkdir(parents=True)
        (old_db / "old.txt").write_text("old", encoding="utf-8")
        unrelated = target / "rag/dbs/unrelated-rag"
        unrelated.mkdir(parents=True)
        (unrelated / "keep.txt").write_text("keep", encoding="utf-8")

        result = packages.import_package(package, target)

        self.assertEqual("imported", result["status"])
        self.assertTrue((target / "rag/query/setup.py").is_file())
        self.assertFalse((target / "install.sh").exists())
        self.assertFalse((target / "install.ps1").exists())
        self.assertFalse((target / "rag/dbs/demo-rag/old.txt").exists())
        self.assertTrue((target / "rag/dbs/demo-rag/db.json").is_file())
        self.assertEqual(
            "keep",
            (unrelated / "keep.txt").read_text(encoding="utf-8"),
        )
        self.assertEqual([], list((target / "rag/dbs").glob(".*.previous")))

    def test_older_bootstrap_package_is_imported_without_executing_bootstrap(self) -> None:
        package = self.root / "legacy-package"
        source = self.root / "legacy-source"
        source.mkdir()
        bootstrap = source / "bootstrap.py"
        bootstrap.write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")
        setup = source / "setup.py"
        setup.write_text("print('setup')\n", encoding="utf-8")
        packages._stage_package(
            package,
            [
                packages._Entry(bootstrap, "bootstrap.py"),
                packages._Entry(setup, ".copilot/rag/query/setup.py"),
            ],
            kind=packages._DISTRIBUTION_KIND,
            databases=[],
            created="2026-07-30T00:00:00Z",
            tool_version="legacy",
        )
        target = self.root / "legacy-target"

        result = packages.import_package(package, target)

        self.assertEqual("imported", result["status"])
        self.assertTrue((target / "rag/query/setup.py").is_file())
        self.assertFalse((target / "bootstrap.py").exists())

    def test_setup_restores_db_relative_and_teams_paths_after_copy(self) -> None:
        rag = self.root / ".copilot/rag"
        db = rag / "dbs/demo-rag"
        logs = db / "logs"
        source_key = "src_teams-0123456789ab"
        source_dir = db / "sources" / source_key
        logs.mkdir(parents=True)
        source_dir.mkdir(parents=True)
        shared_root = self.root / "SharePoint"
        shared_root.mkdir()
        (rag / "config").mkdir(parents=True)
        (rag / "config/source-connections.json").write_text(
            json.dumps(
                {
                    "schema_version": "local-rag.source-connections.v1",
                    "sharepoint_root": str(shared_root),
                    "redmine": {},
                }
            ),
            encoding="utf-8",
        )
        (source_dir / "source.json").write_text(
            json.dumps(
                {
                    "source_type": "teams",
                    "source_id": source_key,
                    "local_source_key": source_key,
                    "fetch": {
                        "root_env": "LOCAL_RAG_SHAREPOINT_ROOT",
                        "relative_path": "Team A/General",
                    },
                }
            ),
            encoding="utf-8",
        )
        (logs / "state.json").write_text(
            json.dumps(
                {
                    "root": {
                        "__local_rag_db_relative_path__": "sources/"
                        + source_key
                        + "/work/ingest"
                    },
                    "scan_root": {
                        "__local_rag_sharepoint_source_key__": source_key,
                        "source_relative_suffix": "Docs",
                    },
                }
            ),
            encoding="utf-8",
        )

        result = restore_copied_installation(rag)
        state = json.loads((logs / "state.json").read_text(encoding="utf-8"))

        self.assertEqual(2, result["values_restored"])
        self.assertEqual(
            str(db / "sources" / source_key / "work/ingest"),
            state["root"],
        )
        self.assertEqual(
            str(shared_root / "Team A" / "General" / "Docs"),
            state["scan_root"],
        )

    def test_setup_leaves_external_marker_until_machine_root_is_registered(self) -> None:
        rag = self.root / ".copilot/rag"
        db = rag / "dbs/demo-rag"
        logs = db / "logs"
        source_key = "src_sharepoint-abcdef012345"
        source_dir = db / "sources" / source_key
        logs.mkdir(parents=True)
        source_dir.mkdir(parents=True)
        (source_dir / "source.json").write_text(
            json.dumps(
                {
                    "source_type": "sharepoint",
                    "source_id": source_key,
                    "local_source_key": source_key,
                    "fetch": {
                        "root_env": "LOCAL_RAG_SHAREPOINT_ROOT",
                        "relative_path": "Docs",
                    },
                }
            ),
            encoding="utf-8",
        )
        marker = {
            "__local_rag_sharepoint_source_key__": source_key,
            "source_relative_suffix": "Sub",
        }
        (logs / "state.json").write_text(
            json.dumps({"scan_root": marker}),
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {}, clear=True):
            result = restore_copied_installation(rag)

        state = json.loads((logs / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(marker, state["scan_root"])
        self.assertEqual(1, result["unresolved_external_roots"])


if __name__ == "__main__":
    unittest.main()
