from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


RAG_ROOT = Path(__file__).resolve().parents[1]
COPILOT_HOME = RAG_ROOT.parent
if str(RAG_ROOT) not in sys.path:
    sys.path.insert(0, str(RAG_ROOT))

from source_manager import packages  # noqa: E402


class DistributionPackageSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": "",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_distribution_is_complete_and_fresh_processes_start(self) -> None:
        archive = self.root / "distribution.zip"
        with mock.patch.object(
            packages,
            "_database_entries",
            return_value=([], []),
        ):
            result = packages.create_distribution_package(
                COPILOT_HOME,
                archive,
            )
        manifest = result["manifest"]
        paths = {
            str(record["path"])
            for record in manifest["files"]
        }
        required = {
            ".copilot/rag/setup.py",
            ".copilot/rag/query/reference_contract.py",
            (
                ".copilot/rag/gen_db/software_rag_tool/"
                "software_rag_tool/chunking.py"
            ),
            (
                ".copilot/rag/gen_db/software_rag_tool/"
                "software_rag_tool/extractors.py"
            ),
            (
                ".copilot/rag/gen_db/software_rag_tool/"
                "software_rag_tool/ingestion_paths.py"
            ),
            (
                ".copilot/rag/gen_db/software_rag_tool/"
                "software_rag_tool/records.py"
            ),
            ".copilot/rag/docs/local-rag-manager-guide-ja.md",
            ".copilot/skills/local-rag/SKILL.md",
            ".copilot/skills/local-rag-setup/SKILL.md",
        }
        self.assertTrue(required.issubset(paths))
        self.assertNotIn("bootstrap.py", paths)
        self.assertNotIn(".copilot/rag/manage.py", paths)
        self.assertNotIn(".copilot/rag/source_manager/packages.py", paths)
        self.assertNotIn(
            (
                ".copilot/rag/gen_db/software_rag_tool/"
                "scripts/index_build.py"
            ),
            paths,
        )
        self.assertFalse(
            any(
                path.startswith(".copilot/rag/query/test_")
                for path in paths
            )
        )
        config_paths = {
            path
            for path in paths
            if path.startswith(".copilot/rag/config/")
        }
        self.assertEqual(
            {
                ".copilot/rag/config/manage-custom.example.json",
                ".copilot/rag/config/network.example.json",
            },
            config_paths,
        )

        unpacked = self.root / "unpacked"
        unpacked.mkdir()
        packages._extract_distribution_zip(
            archive,
            unpacked,
            expected_kind=packages._DISTRIBUTION_KIND,
        )
        commands = (
            [sys.executable, ".copilot/rag/setup.py", "--help"],
            [
                sys.executable,
                ".copilot/rag/list_dbs.py",
                "--format",
                "json",
            ],
            [sys.executable, ".copilot/rag/search.py", "--help"],
            [
                sys.executable,
                ".copilot/rag/query/result_detail.py",
                "--help",
            ],
        )
        for command in commands:
            completed = subprocess.run(
                command,
                cwd=unpacked,
                env=self.environment,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            self.assertEqual(
                0,
                completed.returncode,
                msg=(
                    f"{command} failed\n"
                    f"stdout:\n{completed.stdout}\n"
                    f"stderr:\n{completed.stderr}"
                ),
            )
        listed = subprocess.run(
            [
                sys.executable,
                ".copilot/rag/list_dbs.py",
                "--format",
                "json",
            ],
            cwd=unpacked,
            env=self.environment,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=30,
        )
        self.assertEqual([], json.loads(listed.stdout)["databases"])

    def test_admin_transfer_starts_management_entrypoints(self) -> None:
        output = self.root / "admin-transfer"
        with mock.patch.object(
            packages,
            "_database_entries",
            return_value=([], []),
        ):
            result = packages.create_admin_transfer_package(
                COPILOT_HOME,
                output,
            )
        paths = {
            str(record["path"])
            for record in result["manifest"]["files"]
        }
        required = {
            ".copilot/rag/manage.py",
            ".copilot/rag/source_manager/packages.py",
            ".copilot/rag/gen_db/add_data.py",
            (
                ".copilot/rag/gen_db/software_rag_tool/"
                "software_rag_tool/incremental.py"
            ),
            (
                ".copilot/rag/gen_db/software_rag_tool/"
                "software_rag_tool/source_inventory.py"
            ),
        }
        self.assertTrue(required.issubset(paths))
        self.assertNotIn("bootstrap.py", paths)
        self.assertFalse(
            any("/tests/" in path or "/test_" in path for path in paths)
        )
        commands = (
            [
                sys.executable,
                ".copilot/rag/gen_db/add_data.py",
                "--help",
            ],
            [
                sys.executable,
                ".copilot/rag/gen_db/rebuild_component.py",
                "--help",
            ],
        )
        for command in commands:
            completed = subprocess.run(
                command,
                cwd=output,
                env=self.environment,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            self.assertEqual(
                0,
                completed.returncode,
                msg=(
                    f"{command} failed\n"
                    f"stdout:\n{completed.stdout}\n"
                    f"stderr:\n{completed.stderr}"
                ),
            )
        manager = subprocess.run(
            [sys.executable, ".copilot/rag/manage.py"],
            cwd=output,
            env=self.environment,
            input="0\n",
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        self.assertEqual(
            0,
            manager.returncode,
            msg=f"stdout:\n{manager.stdout}\nstderr:\n{manager.stderr}",
        )

    def test_product_policy_defaults_to_include_for_future_runtime(self) -> None:
        future_query = RAG_ROOT / "query" / "future_runtime.py"
        future_tool = (
            RAG_ROOT
            / "gen_db"
            / "software_rag_tool"
            / "software_rag_tool"
            / "future_runtime.py"
        )
        self.assertTrue(
            packages._product_payload_file(
                future_query,
                rag_root=RAG_ROOT,
                admin=False,
            )
        )
        self.assertTrue(
            packages._product_payload_file(
                future_tool,
                rag_root=RAG_ROOT,
                admin=False,
            )
        )
        self.assertFalse(
            packages._product_payload_file(
                RAG_ROOT / "query" / "test_future_runtime.py",
                rag_root=RAG_ROOT,
                admin=False,
            )
        )
        self.assertFalse(
            packages._product_payload_file(
                RAG_ROOT / "config" / "network.json",
                rag_root=RAG_ROOT,
                admin=True,
            )
        )
        self.assertFalse(
            packages._product_payload_directory(
                RAG_ROOT / "user-exports",
                rag_root=RAG_ROOT,
                admin=False,
            )
        )
        self.assertFalse(
            packages._product_payload_file(
                (
                    RAG_ROOT
                    / "gen_db"
                    / "software_rag_tool"
                    / "scripts"
                    / "future_admin.py"
                ),
                rag_root=RAG_ROOT,
                admin=False,
            )
        )

    def test_secret_patterns_in_product_trees_fail_closed(self) -> None:
        source = self.root / "payload"
        source.mkdir()
        (source / ".env.production").write_text(
            "TOKEN=must-not-ship\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            packages.PackageError,
            "forbidden_package_source",
        ):
            packages._add_tree(
                [],
                source,
                ".copilot/rag/query",
                include=lambda _path: True,
            )

        self.assertFalse(packages._is_secret_path(source / ".env.example"))
        self.assertTrue(packages._is_secret_path(source / ".env.production"))

    def test_product_tree_rejects_links_and_windows_reparse_points(self) -> None:
        source = self.root / "payload"
        external = self.root / "external"
        source.mkdir()
        external.mkdir()
        (external / "outside.py").write_text(
            "VALUE = 'outside'\n",
            encoding="utf-8",
        )
        linked = source / "linked"
        try:
            linked.symlink_to(external, target_is_directory=True)
        except (NotImplementedError, OSError):
            self.skipTest("directory symlinks are unavailable")

        with self.assertRaisesRegex(
            packages.PackageError,
            "package_symlink_forbidden",
        ):
            packages._add_tree(
                [],
                source,
                ".copilot/rag/query",
                include=lambda _path: True,
            )

        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if reparse:
            metadata = SimpleNamespace(
                st_mode=stat.S_IFDIR,
                st_file_attributes=reparse,
            )
            self.assertTrue(
                packages._is_link_or_reparse(source, metadata)
            )

    def test_changed_product_files_are_rescanned_for_credentials(self) -> None:
        cases = (
            (
                "runtime.py",
                "VALUE = 'safe'\n",
                "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n",
                "forbidden_package_source",
            ),
            (
                ".gitmodules",
                '[submodule "docs"]\npath = docs\nurl = ../docs.git\n',
                (
                    "[http]\n"
                    "extraHeader = Authorization: Bearer must-not-ship\n"
                ),
                "credential_configuration_detected",
            ),
        )
        for index, (name, initial, changed, error) in enumerate(cases):
            with self.subTest(name=name):
                source = self.root / f"changed-{index}"
                source.mkdir()
                candidate = source / name
                candidate.write_text(initial, encoding="utf-8")
                entries: list[packages._Entry] = []
                packages._add_tree(
                    entries,
                    source,
                    ".copilot/rag/query",
                    include=lambda _path: True,
                )
                candidate.write_text(changed, encoding="utf-8")

                with self.assertRaisesRegex(packages.PackageError, error):
                    packages._stage_package(
                        self.root / f"changed-stage-{index}",
                        entries,
                        kind=packages._DISTRIBUTION_KIND,
                        databases=[],
                        created="2026-07-30T00:00:00Z",
                        tool_version="test",
                    )

    def test_parent_link_swap_after_collection_is_rejected(self) -> None:
        source = self.root / "race-source"
        query = source / "query"
        external = self.root / "race-external"
        query.mkdir(parents=True)
        external.mkdir()
        (query / "runtime.py").write_text(
            "VALUE = 'safe'\n",
            encoding="utf-8",
        )
        (external / "runtime.py").write_text(
            "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n",
            encoding="utf-8",
        )
        entries: list[packages._Entry] = []
        packages._add_tree(
            entries,
            source,
            ".copilot/rag",
            include=lambda _path: True,
        )
        query.rename(source / "query-original")
        try:
            query.symlink_to(external, target_is_directory=True)
        except (NotImplementedError, OSError):
            self.skipTest("directory symlinks are unavailable")

        with self.assertRaisesRegex(
            packages.PackageError,
            "package_symlink_forbidden",
        ):
            packages._stage_package(
                self.root / "race-stage",
                entries,
                kind=packages._DISTRIBUTION_KIND,
                databases=[],
                created="2026-07-30T00:00:00Z",
                tool_version="test",
            )


if __name__ == "__main__":
    unittest.main()
